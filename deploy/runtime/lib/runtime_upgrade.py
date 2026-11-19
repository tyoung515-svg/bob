"""Root-only journaled runtime upgrade. No provider or network operations."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from upgrade_payload import MANIFEST, VERSION, canonical, unpack, owned, sha

PHASES = ('preflight', 'backup', 'stop', 'swap', 'units', 'migrations', 'refresh-env',
          'hooks', 'start', 'health', 'publish', 'done')
TERMINAL = {'done', 'rolled_back'}
FAILURE_CODES = frozenset({'runtime_too_old', 'runtime_unhealthy',
                           'insufficient_space', 'unsupported_layout'})


class UpgradeRefusal(ValueError):
    """Only fixed, non-secret refusal codes may reach the public receipt."""
    def __init__(self, code, message):
        if code not in FAILURE_CODES:
            raise ValueError('invalid upgrade refusal code')
        super().__init__(message)
        self.failure_code = code


def record_failure(state, exc):
    # Never inspect/stringify exception text, including wrapped provider errors.
    state.update(failure=type(exc).__name__, failure_phase=state['phase'])
    state.pop('failure_code', None)
    if isinstance(exc, UpgradeRefusal) and exc.failure_code in FAILURE_CODES:
        state['failure_code'] = exc.failure_code


def sync_dir(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def write(path, data, mode=0o600):
    temporary = path.with_name('.' + path.name + '.new')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(mode)
    os.replace(temporary, path)
    sync_dir(path.parent)


def remove(path):
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def copy(source, target):
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    else:
        shutil.copy2(source, target)


def durable(path):
    """Flush staged trees before publishing their rename/journal receipt."""
    if os.name == 'nt' or path.is_symlink():
        return
    if path.is_dir():
        for child in path.iterdir():
            durable(child)
        sync_dir(path)
    else:
        with path.open('rb') as stream:
            os.fsync(stream.fileno())


def version_at(root):
    path = root / VERSION
    return json.loads(path.read_bytes()) if path.exists() else dict(
        build_id='legacy', build_epoch=0, git_rev='unknown', payload_sha256=None)


def newer(current, candidate, force=False):
    if not force and (candidate['build_id'] == current['build_id']
                      or candidate['build_epoch'] <= current['build_epoch']):
        raise ValueError('same version or downgrade refused; root may use --force')


class Upgrade:
    def __init__(self, root=Path('/'), operations=None):
        self.root = root
        self.store = root / 'var/lib/bob-upgrades'
        self.ops = operations or LinuxOperations()

    def target(self, name):
        if not (owned(name) or name in ('/' + VERSION, '/' + MANIFEST)):
            raise ValueError('destination outside runtime allowlist')
        path = self.root / name.lstrip('/')
        # Never follow operator/malicious symlinks into lane data, including parents.
        for parent in (path, *path.parents):
            if parent == self.root.parent:
                break
            if parent.is_symlink():
                # A payload symlink leaf is replaced, never dereferenced.
                if parent != path:
                    raise ValueError('symlink in runtime destination parent')
        return path

    def private_store(self):
        for path in (self.store, *self.store.parents):
            if path == self.root.parent:
                break
            if path.is_symlink():
                raise ValueError('unsafe upgrade journal directory')
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt' and (self.store.stat().st_uid != os.geteuid() or self.store.stat().st_mode & 0o077):
            raise ValueError('upgrade journal must be root-private')

    def journal(self, identifier):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,200}', identifier):
            raise ValueError('invalid upgrade ID')
        path = self.store / identifier
        if path.is_symlink():
            raise ValueError('unsafe journal')
        return path

    def status(self):
        result = dict(installed=version_at(self.root), upgrade=None)
        latest = self.store / 'latest'
        identifier = latest.read_text().strip() if latest.exists() else None
        state = (json.loads((self.journal(identifier) / 'state.json').read_bytes())
                 if identifier else None)
        attempt = self.store / 'latest-attempt'
        if attempt.exists():
            attempted = json.loads((self.journal(attempt.read_text().strip()) / 'state.json').read_bytes())
            # A refusal belongs to the real upgrade it followed. Ignore a stale
            # attempt pointer after a crash publishing a newer real upgrade.
            if ('refused_after' in attempted and attempted['refused_after'] == identifier
                    and (state is None or state['phase'] in TERMINAL)):
                state = attempted
        if state is not None:
            result['upgrade'] = {k: state[k] for k in ('id', 'phase', 'outcome', 'from', 'to')}
            if state.get('failure_code') in FAILURE_CODES:
                result['upgrade']['failure_code'] = state['failure_code']
        return result

    def save(self, folder, state):
        write(folder / 'state.json', canonical(state) + b'\n')
        print(json.dumps({k: state[k] for k in ('id', 'phase', 'outcome')}), flush=True)

    def prepare(self, archive, digest, force):
        self.private_store()
        latest = self.store / 'latest'
        if latest.exists():
            folder = self.journal(latest.read_text().strip())
            state = json.loads((folder / 'state.json').read_bytes())
            if state['phase'] not in TERMINAL:
                if state['archive_sha256'] != digest:
                    raise ValueError('another upgrade is unfinished; resume it or roll it back')
                return folder, state
        # Stage privately BEFORE trusting any byte of an app-provided rootfs.
        pending = self.store / ('staging-' + uuid.uuid4().hex)
        pending.mkdir(mode=0o700)
        current, candidate = None, None
        try:
            current = version_at(self.root)
            staged_archive = pending / 'release.tar.gz'
            if archive.stat().st_size * 4 > shutil.disk_usage(self.store).free:
                raise UpgradeRefusal('insufficient_space', 'insufficient archive staging space')
            shutil.copyfile(archive, staged_archive)
            payload = pending / 'payload'
            payload.mkdir()
            manifest, candidate = unpack(staged_archive, payload, digest)
            staged_archive.unlink()
            current = version_at(self.root)
            newer(current, candidate, force)
            paths = sorted(set(manifest['roots']) | {'/' + VERSION, '/' + MANIFEST})
            # Removed packaged roots are removed on upgrade and restored on rollback.
            old = self.root / MANIFEST
            if old.exists():
                old_manifest = json.loads(old.read_bytes())
                for path in old_manifest['roots']:
                    self.target(path)
                    paths.append(path)
            paths = sorted(set(paths))
            if any(a != b and b.startswith(a + '/') for a in paths for b in paths):
                raise UpgradeRefusal('unsupported_layout', 'runtime payload root layout changed; explicit migration required')
            for path in paths:
                self.target(path)
            identifier = current['build_id'][:64] + '-' + candidate['build_id'][:64] + '-' + uuid.uuid4().hex[:8]
            folder = self.journal(identifier)
            os.rename(pending, folder)
            state = dict(id=identifier, phase='preflight', outcome='running',
                         archive_sha256=digest, **{'from': current, 'to': candidate}, paths=paths,
                         existed={}, swapped=[], migrations_started=False, units=[], enabled=[],
                         quiesced=False, start_new=[], gates=[], preserve=[])
            # This is a packaged DEFAULT, also edited by embedder selection.
            # Retain an operator-selected dimension/model instead of resetting it.
            slots = self.target('/etc/bob/memory_slots.toml')
            default = self.root / 'opt/bob/deploy/runtime/config/memory_slots.toml'
            if slots.exists() and (not default.exists() or sha(slots) != sha(default)):
                state['preserve'].append('/etc/bob/memory_slots.toml')
            # A recovery entrypoint survives restoring C4's old CLI/library.
            runner = folder / 'runner'
            runner.mkdir()
            shutil.copy2(__file__, runner / 'runtime_upgrade.py')
            import upgrade_payload
            shutil.copy2(upgrade_payload.__file__, runner / 'upgrade_payload.py')
            durable(folder)
            self.save(folder, state)
            write(latest, (identifier + '\n').encode())
            write(self.store / 'latest-attempt', (identifier + '\n').encode())
            return folder, state
        except BaseException as exc:
            remove(pending)
            if isinstance(exc, UpgradeRefusal):
                # These refusals can occur before a payload/candidate is trusted.
                # Publish a terminal receipt without touching installed payloads.
                try:
                    identifier = 'refused-' + uuid.uuid4().hex
                    folder = self.journal(identifier)
                    folder.mkdir(mode=0o700)
                    state = dict(id=identifier, phase='preflight', outcome='running',
                                 **{'from': current, 'to': candidate or dict(build_id='unknown', build_epoch=0,
                                     git_rev='unknown', payload_sha256=None)}, quiesced=False,
                                 refused_after=latest.read_text().strip() if latest.exists() else None)
                    record_failure(state, exc)
                    state.update(phase='rolled_back', outcome='rolled_back')
                    self.save(folder, state)
                    # Keep the last real upgrade recoverable under the existing
                    # latest guard; a staging refusal changed no installed files.
                    write(self.store / 'latest-attempt', (identifier + '\n').encode())
                except OSError:
                    pass  # e.g. a full disk: the refusal itself still propagates.
            raise

    def backup(self, folder, state):
        saved = folder / 'files'
        # A phase receipt is published only after all copies AND the dump complete.
        remove(saved)
        saved.mkdir()
        for name in state['paths']:
            target = self.target(name)
            exists = target.exists() or target.is_symlink()
            state['existed'][name] = exists
            if exists:
                destination = saved / name.lstrip('/')
                destination.parent.mkdir(parents=True, exist_ok=True)
                copy(target, destination)
        self.ops.backup_controls(folder)
        self.ops.dump(folder)
        durable(saved)
        sync_dir(folder)

    def swap(self, folder, state):
        for name in state['paths']:
            if name == '/' + VERSION or name in state['preserve']:
                continue  # Publish the version only after the health gate.
            if name in state['swapped']:
                continue
            target = self.target(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            old = target.with_name('.' + target.name + '-' + state['id'] + '.old')
            new = target.with_name('.' + target.name + '-' + state['id'] + '.new')
            source = folder / 'payload' / name.lstrip('/')
            if not state['existed'][name] and (target.exists() or target.is_symlink()):
                state['swapped'].append(name)  # Crash after publishing a newly introduced root.
                self.save(folder, state)
                continue
            # Rebuild an interrupted staging copy. .old proves the first rename occurred.
            if not old.exists() and not old.is_symlink():
                remove(new)
                if source.exists() or source.is_symlink():
                    copy(source, new)
                    durable(new)
                if target.exists() or target.is_symlink():
                    os.rename(target, old)
                    sync_dir(target.parent)
            if new.exists() or new.is_symlink():
                os.replace(new, target)
                sync_dir(target.parent)
            elif source.exists() and not target.exists():
                # New root, interrupted before its rename; no old root exists.
                copy(source, new)
                durable(new)
                os.replace(new, target)
                sync_dir(target.parent)
            state['swapped'].append(name)
            self.save(folder, state)

    def rollback(self, folder, state):
        write(self.store / 'latest-attempt', (state['id'] + '\n').encode())
        if state['phase'] == 'rolled_back':
            return self.status()
        if not state['quiesced']:
            state.update(phase='rolled_back', outcome='rolled_back')
            self.save(folder, state)
            return self.status()  # No live runtime mutation occurred before quiescence.
        state.update(phase='rollback', outcome='rolling_back')
        self.save(folder, state)
        try:
            self.ops.block(folder, state, lambda: self.save(folder, state))
            self.ops.stop(state)
            # Undo enablement while newly introduced templates still exist.
            # Also safe on retry after an older runner already removed them.
            self.ops.deactivate_new(state)
            for name in state['paths']:
                if name not in state['existed']:
                    continue  # Preflight/backup never mutated this target.
                target = self.target(name)
                old = target.with_name('.' + target.name + '-' + state['id'] + '.old')
                new = target.with_name('.' + target.name + '-' + state['id'] + '.new')
                source = folder / 'files' / name.lstrip('/')
                # The original .old inode survives a crash between either rename.
                if not state['existed'][name]:
                    remove(target)
                    remove(old)
                elif old.exists() or old.is_symlink():
                    remove(target)
                    os.rename(old, target)
                elif state['existed'][name] and (source.exists() or source.is_symlink()):
                    remove(new)
                    copy(source, new)
                    durable(new)
                    remove(target)
                    os.rename(new, target)
                remove(new)
                if target.parent.is_dir():
                    sync_dir(target.parent)
            self.ops.reload()
            self.ops.restore_controls(folder, state)
            if state['migrations_started']:
                self.ops.restore_db(folder)
            self.ops.reload()
            self.ops.refresh()
            self.ops.unblock(state)
            self.ops.reload()
            self.ops.start(state, lambda: self.save(folder, state))
            self.ops.health()
            state.update(phase='rolled_back', outcome='rolled_back')
            self.save(folder, state)
        except BaseException as exc:
            state.update(outcome='rollback_failed', rollback_failure=type(exc).__name__)
            self.save(folder, state)
            raise
        return self.status()

    def run(self, archive, digest, force=False):
        folder, state = self.prepare(archive, digest, force)
        if state['phase'] == 'rollback':
            return self.rollback(folder, state)
        try:
            start = PHASES.index(state['phase'])
            if start > PHASES.index('stop') and state['phase'] != 'done':
                self.ops.block(folder, state, lambda: self.save(folder, state))
                self.ops.stop(state)  # A reboot invalidates any prior quiescence.
                if state['phase'] in {'health', 'publish'}:
                    self.ops.unblock(state)
                    self.ops.reload()
                    self.ops.start(state, lambda: self.save(folder, state))
                    self.ops.health()  # Never publish stale pre-reboot health evidence.
            for index in range(start, len(PHASES) - 1):
                phase = PHASES[index]
                if phase == 'preflight':
                    self.ops.preflight(folder, state)
                elif phase == 'backup':
                    self.backup(folder, state)
                elif phase == 'stop':
                    if not state['quiesced']:
                        self.ops.record_active(state, lambda: self.save(folder, state))
                    state['quiesced'] = True
                    self.save(folder, state)
                    self.ops.block(folder, state, lambda: self.save(folder, state))
                    self.ops.stop(state)
                    # Freeze a second, authoritative snapshot after all lane writers stop.
                    self.ops.dump(folder)
                elif phase == 'swap':
                    self.swap(folder, state)
                elif phase == 'units':
                    self.ops.reload()
                    self.ops.enable(folder, state, lambda: self.save(folder, state))
                elif phase == 'migrations':
                    # Conservatively restore DB even if only ONE SQL transaction committed.
                    state['migrations_started'] = True
                    self.save(folder, state)
                    self.ops.migrate()
                elif phase == 'refresh-env':
                    self.ops.refresh()
                elif phase == 'hooks':
                    self.ops.hooks(folder, state, lambda: self.save(folder, state))
                elif phase == 'start':
                    self.ops.unblock(state)
                    self.ops.reload()
                    self.ops.start(state, lambda: self.save(folder, state))
                elif phase == 'health':
                    self.ops.health()
                elif phase == 'publish':
                    write(self.target('/' + VERSION), canonical(state['to']) + b'\n', 0o644)
                state['phase'] = PHASES[index + 1]
                if state['phase'] == 'done':
                    state['outcome'] = 'completed'
                self.save(folder, state)
            # Keep original files and DB dump for manual rollback; drop rename spares.
            for name in state['paths']:
                target = self.target(name)
                remove(target.with_name('.' + target.name + '-' + state['id'] + '.old'))
            return self.status()
        except BaseException as exc:
            # No exception text: subprocess/HTTP errors can contain secret-bearing context.
            record_failure(state, exc)
            self.save(folder, state)
            if not state['quiesced']:
                state.update(phase='rolled_back', outcome='rolled_back')
                self.save(folder, state)
                return self.status()
            self.rollback(folder, state)
            return self.status()


class LinuxOperations:
    @staticmethod
    def command(args, **kwargs):
        return subprocess.run(args, check=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, **kwargs)

    def runtime(self, code):
        return self.command(['/opt/bob/.venv/bin/python', '-I', '-c',
                             "import sys; sys.path.insert(0, '/usr/local/lib/bob_runtime'); " + code])

    def preflight(self, folder, state):
        self.health()
        if not Path('/etc/bob/execution-boundary').exists():
            raise UpgradeRefusal('runtime_too_old', 'isolated runtime migration required before upgrading')
        from firstboot import psql
        size = int(psql("SELECT pg_database_size('bobclaw');"))
        payload_size = sum(p.stat().st_size for p in (folder / 'payload').rglob('*') if p.is_file() and not p.is_symlink())
        old_size = 0
        for name in state['paths']:
            path = Path(name)
            if path.is_dir() and not path.is_symlink():
                if path.is_mount():
                    raise UpgradeRefusal('unsupported_layout', 'mounted runtime payload roots require an explicit migration')
                old_size += sum(p.stat().st_size for p in path.rglob('*') if p.is_file() and not p.is_symlink())
        manifest = json.loads((folder / 'payload' / MANIFEST).read_bytes())
        for row in manifest['entries']:
            if row['kind'] == 'symlink' and row['target'].startswith('/opt/python/') and not Path(row['target']).exists():
                raise UpgradeRefusal('unsupported_layout', 'release requires a Python dependency outside the supported payload')
        size += max(payload_size, old_size)
        if shutil.disk_usage(folder).free < size * 3 + 256 * 1024 * 1024:
            raise UpgradeRefusal('insufficient_space', 'insufficient upgrade/rollback space')
        units = json.loads(self.command(['systemctl', 'list-units', '--all', '--output=json', '--no-pager']).stdout)
        state['units'] = sorted(r['unit'] for r in units if r['active'] in ('active', 'activating')
                                and self.managed_unit(r['unit']))
        # Never launch retained/disabled lanes during upgrade or rollback.
        from lane import lanes
        for _, env in lanes():
            if (env.parent / '.disabled').exists():
                continue
            from worker import load_identity
            try:
                load_identity(env.parent.parent.name)  # Reject pre-isolation/unfinished lanes.
            except ValueError as exc:
                # Invalid JSON/changed identity is not proof of an old runtime.
                from worker import IDENTITIES
                if not (IDENTITIES / (env.parent.parent.name + '.json')).is_file():
                    raise UpgradeRefusal('runtime_too_old', 'lane isolation migration required') from exc
                raise

    @staticmethod
    def managed_unit(name):
        return (bool(re.fullmatch(r'bob-[a-zA-Z0-9@_.-]+\.(service|socket|timer)', name))
                and name not in {'bob-qdrant.service', 'bob-firstboot.service', 'bob-migrate-workers.service',
                                 'bob-runtime-upgrade.service'})

    def backup_controls(self, folder):
        embedder = {}
        for name in ('/etc/bob/embedder.env', '/etc/systemd/system/bob-embedder.service.d/selection.conf'):
            path = Path(name)
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError('symlink in embedder upgrade control path')
            embedder[name] = dict(content=path.read_text(), mode=path.stat().st_mode & 0o777) if path.exists() else None
        write(folder / 'embedder-controls.json', canonical(embedder))
        path = Path('/etc/bob/workers')
        remove(folder / 'worker-controls')  # Replay an interrupted backup phase.
        if path.exists():
            copy(path, folder / 'worker-controls')
        from lane import lanes
        from worker import load_identity
        controls = {}
        for name, _ in lanes():
            key = load_identity(name)['lane_id']
            for relative in (f'bob-egress-proxy@{key}.service.d/identity.conf',
                             f'bob-core@{name}.service.d/egress.conf',
                             f'bob-worker@{key}.service.d/claude.conf',
                             f'bob-claude-turn@{key}.service.d/identity.conf',
                             f'bob-claude-turn@{key}.socket.d/identity.conf'):
                path = Path('/etc/systemd/system') / relative
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise ValueError('symlink in upgrade control path')
                controls[relative] = path.read_text() if path.exists() else None
        write(folder / 'control-files.json', canonical(controls))

    def restore_controls(self, folder, state):
        if (folder / 'embedder-controls.json').exists():
            for name, original in json.loads((folder / 'embedder-controls.json').read_text()).items():
                if name not in ('/etc/bob/embedder.env', '/etc/systemd/system/bob-embedder.service.d/selection.conf'):
                    raise ValueError('invalid embedder upgrade control path')
                path = Path(name)
                if any(p.is_symlink() for p in (path, *path.parents)):
                    raise ValueError('symlink in embedder upgrade control path')
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    write(path, original['content'].encode(), original['mode'])
        if (folder / 'worker-controls').exists():
            for saved in (folder / 'worker-controls').glob('*.json'):
                write(Path('/etc/bob/workers') / saved.name, saved.read_bytes(), saved.stat().st_mode & 0o777)
        if (folder / 'control-files.json').exists():
            for name, original in json.loads((folder / 'control-files.json').read_text()).items():
                path = Path('/etc/systemd/system') / name
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    write(path, original.encode())

    def deactivate_new(self, state):
        for unit in state['enabled']:
            for action in ('stop', 'disable'):
                try:
                    self.command(['systemctl', action, unit])
                except subprocess.CalledProcessError:
                    # A previous rollback attempt may already have restored C4,
                    # which has no new unit template. Never swallow other errors.
                    try:
                        result = self.command(['systemctl', 'show', unit,
                                               '--property=LoadState', '--value'])
                        load = result.stdout
                    except subprocess.CalledProcessError as exc:
                        load = exc.stdout or b''
                    if load.strip() not in (b'not-found', b'not-loaded'):
                        raise

    def dump(self, folder):
        # Open as root; postgres need not traverse the private backup directory.
        temporary = folder / 'bob.dump.new'
        with temporary.open('wb') as output:
            subprocess.run(['runuser', '-u', 'postgres', '--', 'pg_dump', '-Fc', '-d', 'bobclaw'],
                           check=True, stdout=output, stderr=subprocess.PIPE)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, folder / 'bob.dump')
        sync_dir(folder)

    def restore_db(self, folder):
        from firstboot import psql
        # Recreate the database: --clean alone leaves tables introduced AFTER the
        # dump behind. These fixed-name statements are separate autocommit calls.
        # A failed restore keeps lane boot gates closed and is safely retryable.
        psql('DROP DATABASE IF EXISTS bobclaw WITH (FORCE);')
        psql('CREATE DATABASE bobclaw OWNER bobclaw TEMPLATE template0;')
        with (folder / 'bob.dump').open('rb') as stream:
            self.command(['runuser', '-u', 'postgres', '--', 'pg_restore', '--clean', '--if-exists',
                          '--single-transaction', '--exit-on-error', '-d', 'bobclaw'], stdin=stream)

    def block(self, folder, state, persist):
        from common import root_directory
        manifest = json.loads((folder / 'payload' / MANIFEST).read_bytes())
        units = set(state['units']) | {Path(p).name for p in manifest['roots']
                                     if p.startswith('/etc/systemd/system/') and self.managed_unit(Path(p).name)}
        for unit in sorted(units):
            if unit not in state['gates']:
                state['gates'].append(unit)
                persist()
            directory = Path('/etc/systemd/system') / (unit + '.d')
            root_directory(directory)
            write(directory / 'bob-runtime-upgrade.conf',
                  b'[Unit]\nConditionPathExists=!/var/lib/bob-upgrades/pending\n', 0o644)
        write(folder.parent / 'pending', (state['id'] + '\n').encode())
        self.reload()

    def unblock(self, state):
        for unit in state['gates']:
            (Path('/etc/systemd/system') / (unit + '.d') / 'bob-runtime-upgrade.conf').unlink(missing_ok=True)
        pending = Path('/var/lib/bob-upgrades/pending')
        pending.unlink(missing_ok=True)
        sync_dir(pending.parent)

    def socket_activators(self, units):
        result = {}
        for unit in sorted(units):
            if unit.endswith('.service'):
                sockets = self.command(['systemctl', 'show', unit,
                                        '--property=TriggeredBy', '--value']).stdout.decode().split()
                sockets = [s for s in sockets if s.endswith('.socket') and self.managed_unit(s)]
                if sockets:
                    result[unit] = sockets
        return result

    def active_units(self):
        rows = json.loads(self.command(['systemctl', 'list-units', '--all', '--output=json', '--no-pager']).stdout)
        return {r['unit'] for r in rows if self.managed_unit(r['unit'])
                and r['active'] in ('active', 'activating', 'reloading')}

    def record_active(self, state, persist):
        # Freeze the set during stop, before installing boot gates. If a crash
        # follows, replay retains this receipt instead of capturing a gated boot.
        if not state.get('active_units_recorded'):
            active = self.active_units()
            state['units'] = sorted(active)
            state['socket_activators'] = self.socket_activators(active)
            state['active_units_recorded'] = True
            persist()  # Durable BEFORE gates/quiescence can stop anything.

    def stop(self, state):
        # Include newly started packaged units when recovering from a failed
        # health gate, without asking systemd to stop missing/inactive units.
        units = sorted(self.active_units())
        if units:
            self.command(['systemctl', 'stop', *units])

    def reload(self):
        self.command(['systemctl', 'daemon-reload'])

    def enable(self, folder, state, persist):
        manifest = json.loads((folder / 'payload' / MANIFEST).read_bytes())
        for name in manifest['roots']:
            if not name.startswith('/etc/systemd/system/'):
                continue
            unit = Path(name).name
            text = Path(name).read_text()
            # Templates are enabled per lane by explicit hooks. Setup oneshots stay manual.
            if '@' in unit or '[Install]' not in text or unit in {'bob-firstboot.service', 'bob-migrate-workers.service'}:
                continue
            result = subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True)
            if result.returncode and unit not in state['enabled']:
                state['enabled'].append(unit)
                persist()  # Record undo BEFORE mutation.
                self.command(['systemctl', 'enable', unit])
                if unit.endswith('.timer') or (unit.endswith('.service') and 'Type=oneshot' not in text):
                    state['start_new'].append(unit)
                    persist()

    def migrate(self):
        self.runtime('from firstboot import apply_migrations; apply_migrations()')

    def refresh(self):
        self.runtime('import lane, worker; [worker.service_environments(p.parent) for _, p in lane.lanes()]')

    def hooks(self, folder, state, persist):
        result = self.runtime('from upgrade_hooks import run; run()')
        units = json.loads(result.stdout)['units']
        for unit in units:
            if not self.managed_unit(unit):
                raise ValueError('invalid upgrade hook unit')
            if unit not in state['start_new']:
                state['start_new'].append(unit)
                persist()
            if (unit.startswith('bob-egress-proxy@')
                    or unit.startswith('bob-claude-turn@') and unit.endswith('.socket')):
                enabled = subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True)
                if enabled.returncode and unit not in state['enabled']:
                    state['enabled'].append(unit)
                    persist()
                    self.command(['systemctl', 'enable', unit])

    def start(self, state, persist=None):
        units = set(state['units'])
        if state['phase'] != 'rollback':
            units.update(state['start_new'])
        # TriggeredBy also covers explicitly named socket services and Accept=yes
        # instances. Query live metadata for old journals lacking this receipt.
        activators = dict(state.get('socket_activators', {}))
        activators.update(self.socket_activators(units))
        for service, sockets in activators.items():
            if service in units:
                units.remove(service)
                units.update(sockets)
        if not units:
            return
        failed = False
        try:
            # One transaction lets systemd honor Requires=/After= dependencies.
            self.command(['systemctl', 'start', *sorted(units)])
        except subprocess.CalledProcessError:
            failed = True
        results = []
        for unit in sorted(units):
            args = ['systemctl', 'show', unit, '--property=ActiveState',
                    '--property=SubState', '--property=Type', '--property=Result']
            try:
                data = self.command(args).stdout
            except subprocess.CalledProcessError as exc:
                data = exc.stdout or b''
            data = data.decode()
            props = dict(line.split('=', 1) for line in data.splitlines() if '=' in line)
            active = props.get('ActiveState')
            ok = active in ('active', 'activating') or (not failed and
                props.get('Type') == 'oneshot' and props.get('Result') == 'success'
                and active == 'inactive')
            result = dict(unit=unit, active=active, sub=props.get('SubState'),
                          service_result=props.get('Result'), ok=ok)
            results.append(result)
            print(json.dumps(dict(unit_start=result)), flush=True)
        state['unit_start_results'] = results
        if persist:
            persist()
        # An already active (exited) oneshot is success even if the aggregate
        # invocation returned nonzero; a failed/inactive daemon remains fatal.
        if any(not r['ok'] for r in results):
            raise ValueError('runtime unit activation failed' if failed else 'runtime unit inactive after start')

    def health(self):
        deadline = time.monotonic() + 120
        while True:
            try:
                report = json.loads(self.command(['bob-health']).stdout)
                if report.get('healthy') is True:
                    return
            except (subprocess.CalledProcessError, ValueError):
                pass
            if time.monotonic() >= deadline:
                raise UpgradeRefusal('runtime_unhealthy', 'runtime health gate failed')
            time.sleep(2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--payload', type=Path)
    action.add_argument('--status', action='store_true')
    action.add_argument('--rollback')
    action.add_argument('--resume')
    parser.add_argument('--expected-sha256')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        raise ValueError('root required')
    os.umask(0o077)
    upgrade = Upgrade()
    if args.status:
        print(json.dumps(upgrade.status()))
        return 0
    import fcntl
    # Serialize against lane/settings/migration writers and against another upgrade.
    with open('/run/bob-lane.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.rollback or args.resume:
            identifier = args.rollback or args.resume
            folder = upgrade.journal(identifier)
            state = json.loads((folder / 'state.json').read_bytes())
            latest = (upgrade.store / 'latest').read_text().strip()
            if latest != identifier:
                raise ValueError('only the latest upgrade can be recovered')
            if args.resume:
                if state['phase'] in TERMINAL:
                    raise ValueError('upgrade is already terminal')
                result = upgrade.run(Path('/unused-resume-source'), state['archive_sha256'])
            else:
                result = upgrade.rollback(folder, state)
        else:
            if not args.expected_sha256:
                parser.error('--payload requires --expected-sha256')
            result = upgrade.run(args.payload, args.expected_sha256, args.force)
    print(json.dumps(result), flush=True)
    outcome = result['upgrade']['outcome']
    return 0 if outcome == 'completed' or (args.rollback and outcome == 'rolled_back') else 1


if __name__ == '__main__':
    sys.path[:0] = ['/usr/local/lib/bob_runtime', '/opt/bob/bobclaw-core']
    try:
        raise SystemExit(main())
    except Exception as exc:
        sys.exit('bob-runtime upgrade: ' + type(exc).__name__ + '; inspect the private upgrade journal')
