"""Offline upgrade failure/replay contracts; no WSL, providers or real database."""
import copy
from contextlib import nullcontext
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tarfile
from types import SimpleNamespace

import pytest

LIB = Path(__file__).resolve().parents[1] / 'lib'


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(LIB))
    import upgrade_payload as payload
    import runtime_upgrade as upgrade
    return payload, upgrade


def file(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return path


def package(root, modules, build_id='c5', epoch=5, mutate=None):
    payload, _ = modules
    source = root / ('image-' + build_id)
    file(source, 'opt/bob/code.py', 'new-code')
    file(source, 'opt/bob/migrations/2026-11-06_builder_sessions.sql', 'SELECT 1;')
    file(source, 'usr/local/lib/bob_runtime/runtime_upgrade.py', '# new helper')
    file(source, 'usr/local/bin/bob-runtime', '# new cli')
    file(source, 'etc/systemd/system/bob-core@.service', '[Service]\nExecStart=/bin/true\n')
    file(source, 'etc/bob/memory_slots.toml', '# default')
    file(source, 'var/lib/bob/family/owner/lane.env', 'MUST_NOT_BE_EXTRACTED')
    manifest = payload.build(source, build_id, 'a' * 40, epoch)
    if mutate:
        mutate(manifest, source)
    archive = root / (build_id + '.tar.gz')
    with tarfile.open(archive, 'w:gz') as tar:
        for path in sorted(source.rglob('*')):
            info = tar.gettarinfo(str(path), arcname=path.relative_to(source).as_posix())
            info.uid = info.gid = 0
            if path.is_file():
                with path.open('rb') as stream:
                    tar.addfile(info, stream)
            else:
                tar.addfile(info)
    return archive, payload.sha(archive), manifest


class FakeOperations:
    def __init__(self, root, failure=None):
        self.root, self.failure = root, failure
        self.calls, self.db, self.active = [], 'old-db', True

    def event(self, name):
        self.calls.append(name)
        if name == self.failure:
            self.failure = None
            raise ValueError('synthetic failure with secret text')

    def preflight(self, folder, state):
        self.event('preflight')
        state['units'] = ['bob-core@owner.service', 'bob-worker@abc.socket']

    def backup_controls(self, folder):
        self.event('backup-controls')

    def restore_controls(self, folder, state):
        self.event('restore-controls')

    def deactivate_new(self, state):
        self.event('deactivate-new')

    def dump(self, folder):
        self.event('pg_dump')
        (folder / 'bob.dump').write_text(self.db)

    def stop(self, state):
        self.active = False
        self.event('stop')

    def record_active(self, state, persist):
        self.event('record-active')

    def block(self, folder, state, persist):
        self.event('block')

    def unblock(self, state):
        self.event('unblock')

    def reload(self):
        self.event('reload')

    def enable(self, folder, state, persist):
        self.event('enable')

    def migrate(self):
        self.db = 'new-db'
        self.event('migrate')

    def restore_db(self, folder):
        self.event('pg_restore')
        self.db = (folder / 'bob.dump').read_text()

    def refresh(self):
        self.event('refresh-env')

    def hooks(self, folder, state, persist):
        self.event('hooks')

    def start(self, state, persist=None):
        self.active = True
        self.event('start')

    def health(self):
        # A new version receipt may be published only AFTER this gate.
        if self.db == 'new-db':
            assert json.loads((self.root / 'etc/bob/runtime-version').read_text())['build_id'] == 'c4'
        self.event('health')


@pytest.fixture
def installed(tmp_path, modules):
    root = tmp_path / 'installed'
    file(root, 'opt/bob/code.py', 'old-code')
    file(root, 'etc/bob/runtime-version', json.dumps(dict(build_id='c4', build_epoch=4,
                                                       git_rev='b' * 40, payload_sha256='d' * 64)))
    file(root, 'etc/bob/embedder.env', 'USER_CHOICE=keep')
    file(root, 'var/lib/bob/family/owner/lane.env', 'PASSWORD=keep-secret')
    file(root, 'var/lib/bob/family/owner/projects/keep.html', 'user project')
    return root


def test_manifest_inventory_and_rootfs_extraction_never_select_user_data(tmp_path, modules):
    payload, _ = modules
    archive, digest, manifest = package(tmp_path, modules)
    target = tmp_path / 'unpacked'
    target.mkdir()
    extracted, version = payload.unpack(archive, target, digest)
    assert extracted == manifest
    assert version['build_id'] == 'c5'
    assert all(payload.owned(row['path']) for row in manifest['entries'])
    assert not (target / 'var').exists()
    assert not (target / 'etc/bob/embedder.env').exists()
    assert (target / 'opt/bob/code.py').read_text() == 'new-code'


@pytest.mark.parametrize('path', ['/var/lib/bob', '/var/lib/bob/family/x/lane.env', '/etc/bob/embedder.env',
                                 '/etc/passwd', '/opt/bob/../secret', '//opt/bob', '/usr/local/bin/bob-x/secret',
                                 '/etc/systemd/system/bob-core@.service.d/user.conf', '/opt/bob\\bad'])
def test_path_allowlist_refuses_data_credentials_and_traversal(modules, path):
    assert not modules[0].owned(path)


def test_manifest_tampering_and_archive_digest_refused_before_services(tmp_path, modules, installed):
    payload, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    with pytest.raises(ValueError, match='digest'):
        engine.run(archive, '0' * 64)
    assert not ops.calls
    def corrupt(manifest, source):
        (source / 'opt/bob/code.py').write_text('tampered')
    archive, digest, _ = package(tmp_path, modules, 'c6', 6, corrupt)
    with pytest.raises(ValueError, match='digest'):
        engine.run(archive, digest)
    assert not ops.calls


def test_rehashed_manifest_cannot_authorize_paths_outside_allowlist(tmp_path, modules):
    payload, _ = modules
    _, _, manifest = package(tmp_path, modules)
    manifest['entries'][0]['path'] = '/var/lib/bob/lane.env'
    body = dict(manifest)
    body.pop('payload_sha256')
    manifest['payload_sha256'] = hashlib.sha256(payload.canonical(body)).hexdigest()
    version = {k: manifest[k] for k in ('build_id', 'build_epoch', 'git_rev', 'payload_sha256')}
    with pytest.raises(ValueError):
        payload.validate(manifest, version)


@pytest.mark.parametrize('epoch,build_id', [(3, 'c3'), (4, 'c4'), (5, 'c4')])
def test_downgrade_and_same_version_refused_without_force(tmp_path, modules, installed, epoch, build_id):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules, build_id, epoch)
    ops = FakeOperations(installed)
    with pytest.raises(ValueError, match='downgrade'):
        upgrade.Upgrade(installed, ops).run(archive, digest)
    assert not ops.calls
    upgrade.newer(upgrade.version_at(installed), dict(build_id=build_id, build_epoch=epoch), True)


def test_upgrade_success_and_manual_post_migration_rollback(tmp_path, modules, installed):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    receipt = engine.run(archive, digest)
    assert receipt['upgrade']['outcome'] == 'completed'
    assert (installed / 'opt/bob/code.py').read_text() == 'new-code'
    assert receipt['installed']['build_id'] == 'c5'
    assert ops.calls.index('pg_dump') < ops.calls.index('stop') < ops.calls.index('migrate')
    assert ops.calls.count('pg_dump') == 2
    state_path = engine.store / receipt['upgrade']['id'] / 'state.json'
    state = json.loads(state_path.read_text())
    assert 'secret' not in state_path.read_text()
    restored = engine.rollback(state_path.parent, state)
    assert restored['installed']['build_id'] == 'c4'
    assert restored['upgrade']['outcome'] == 'rolled_back'
    assert ops.db == 'old-db' and ops.active
    assert (installed / 'opt/bob/code.py').read_text() == 'old-code'
    assert (installed / 'var/lib/bob/family/owner/lane.env').read_text() == 'PASSWORD=keep-secret'
    assert (installed / 'var/lib/bob/family/owner/projects/keep.html').read_text() == 'user project'
    assert (installed / 'etc/bob/embedder.env').read_text() == 'USER_CHOICE=keep'
    assert not (installed / 'usr/local/bin/bob-runtime').exists()


@pytest.mark.parametrize('failure,restore', [('enable', False), ('migrate', True), ('hooks', True), ('health', True)])
def test_failure_rolls_back_files_units_and_db_as_required(tmp_path, modules, installed, failure, restore):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed, failure)
    receipt = upgrade.Upgrade(installed, ops).run(archive, digest)
    assert receipt['upgrade']['outcome'] == 'rolled_back'
    assert ('pg_restore' in ops.calls) == restore
    assert ops.active and ops.db == 'old-db'
    assert (installed / 'opt/bob/code.py').read_text() == 'old-code'
    assert receipt['installed']['build_id'] == 'c4'


def test_preflight_failure_does_not_stop_healthy_units(tmp_path, modules, installed):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed, 'preflight')
    receipt = upgrade.Upgrade(installed, ops).run(archive, digest)
    assert receipt['upgrade']['outcome'] == 'rolled_back'
    assert ops.calls == ['preflight'] and ops.active


def test_journal_replays_crash_between_directory_renames(tmp_path, modules, installed, monkeypatch):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    folder, state = engine.prepare(archive, digest, False)
    ops.preflight(folder, state)
    engine.backup(folder, state)
    ops.stop(state)
    state.update(phase='swap', quiesced=True)
    engine.save(folder, state)
    target = engine.target('/opt/bob')
    old = target.with_name('.' + target.name + '-' + state['id'] + '.old')
    new = target.with_name('.' + target.name + '-' + state['id'] + '.new')
    upgrade.copy(folder / 'payload/opt/bob', new)
    os.rename(target, old)  # Power loss before new -> target, no per-path receipt.
    assert not target.exists()
    result = engine.run(archive, digest)
    assert result['upgrade']['outcome'] == 'completed'
    assert result['installed']['build_id'] == 'c5'
    assert (target / 'code.py').read_text() == 'new-code'
    assert ops.calls.count('preflight') == 1


def test_rollback_failure_is_durable_and_manual_retry_resumes(tmp_path, modules, installed):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed, 'migrate')
    engine = upgrade.Upgrade(installed, ops)
    native = ops.restore_db
    def fail(folder):
        raise RuntimeError('restore failed')
    ops.restore_db = fail
    with pytest.raises(RuntimeError):
        engine.run(archive, digest)
    assert engine.status()['upgrade']['outcome'] == 'rollback_failed'
    folder = engine.journal(engine.status()['upgrade']['id'])
    ops.restore_db = native
    engine.rollback(folder, json.loads((folder / 'state.json').read_text()))
    assert engine.status()['upgrade']['outcome'] == 'rolled_back'


def test_firstboot_and_upgrade_share_transactional_ordered_sql_runner(tmp_path, monkeypatch):
    # Load only the SQL runner, with host-specific helpers replaced; no DB calls.
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace())
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace())
    common = SimpleNamespace(DATA=tmp_path, SOURCE=tmp_path, private_write=None,
                             read_env=None, render_env=None, require_root=None)
    monkeypatch.setitem(sys.modules, 'common', common)
    spec = importlib.util.spec_from_file_location('upgrade_firstboot_test', LIB / 'firstboot.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    file(tmp_path, 'init.sql', 'SELECT 0;')
    file(tmp_path, 'migrations/2026-11-06_builder_sessions.sql', 'SELECT 2;')
    file(tmp_path, 'migrations/2026-11-03_worker.sql', 'SELECT 1;')
    applied, order = {}, []
    def psql(sql, database='postgres'):
        if sql.startswith('SELECT sha256'):
            return applied.get(re.search("filename='([^']+)'", sql)[1], '')
        if sql.startswith('BEGIN;'):
            assert sql.endswith('COMMIT;') and 'SET LOCAL ROLE bobclaw;' in sql
            name, digest = re.search("VALUES \\('([^']+)', '([^']+)'\\)", sql).groups()
            applied[name] = digest
            order.append(name)
        return ''
    module.psql = psql
    module.apply_migrations()
    assert order == ['init.sql', 'migrations/2026-11-03_worker.sql', 'migrations/2026-11-06_builder_sessions.sql']
    module.apply_migrations()
    assert len(order) == 3
    (tmp_path / 'init.sql').write_text('SELECT 9;')
    with pytest.raises(ValueError, match='changed'):
        module.apply_migrations()


@pytest.mark.parametrize('shared_limits,lane_limits,expected', [
    ({}, {}, ('600', '300')),
    ({'RATE_LIMIT_PER_MINUTE': '900', 'RATE_LIMIT_BURST': '450'}, {}, ('900', '450')),
    ({'RATE_LIMIT_PER_MINUTE': '900', 'RATE_LIMIT_BURST': '450'},
     {'RATE_LIMIT_PER_MINUTE': '120', 'RATE_LIMIT_BURST': '75'}, ('120', '75')),
    ({}, {'RATE_LIMIT_BURST': '80'}, ('600', '80')),
])
def test_env_refresh_adds_default_without_changing_secrets(tmp_path, monkeypatch,
                                                          shared_limits, lane_limits, expected):
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace())
    monkeypatch.syspath_prepend(str(LIB.parents[2] / 'bobclaw-core'))
    spec = importlib.util.spec_from_file_location('upgrade_common_test', LIB / 'common.py')
    common = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(common)
    monkeypatch.setitem(sys.modules, 'common', common)
    spec = importlib.util.spec_from_file_location('upgrade_worker_test', LIB / 'worker.py')
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    monkeypatch.setattr(common, 'DATA', tmp_path)
    lane = file(tmp_path, 'family/owner/bob-memory/lane.env', common.render_env({
        'BOBCLAW_PASSWORD': 'keep-password', 'JWT_SECRET': 'keep-jwt', 'TOTP_SECRET': 'keep-factor',
        'BOB_BRIDGE_ENGINE': 'http://127.0.0.1:7835', 'BOB_BRIDGE_PORT': '8902', **lane_limits}))
    original = lane.read_bytes()
    shared = file(tmp_path, 'secrets/bobclaw.env', common.render_env(
        {'POSTGRES_URL': 'keep-url', 'REDIS_URL': 'keep-redis', **shared_limits}))
    shared_original = shared.read_bytes()
    worker.load_identity = lambda name: dict(service_gid=12)
    worker.private_replace = lambda path, data, *_: path.write_text(data)
    worker.service_environments(lane.parent)
    assert lane.read_bytes() == original
    values = common.read_env(lane.parent / 'gateway.env')
    assert values['BOB_OWNER_WORK_APPROVAL'] == 'signed_in_user'
    assert (values['RATE_LIMIT_PER_MINUTE'], values['RATE_LIMIT_BURST']) == expected
    assert shared.read_bytes() == shared_original
    # The upgrade refresh calls the same generator; repeated upgrades are idempotent.
    worker.service_environments(lane.parent)
    assert common.read_env(lane.parent / 'gateway.env') == values
    assert lane.read_bytes() == original
    assert values['BOBCLAW_PASSWORD'] == 'keep-password' and values['JWT_SECRET'] == 'keep-jwt'
    assert values['TOTP_SECRET'] == 'keep-factor' and values['POSTGRES_URL'] == 'keep-url'
    assert 'BOBCLAW_PASSWORD' not in common.read_env(lane.parent / 'core.env')


def test_image_build_emits_version_manifest_and_app_sidecar():
    runtime = LIB.parent
    build = (runtime / 'build.sh').read_text()
    image = (runtime / 'Containerfile').read_text()
    assert 'upgrade_payload.py' in image and 'test -s /etc/bob/runtime-version' in image
    assert 'test -s /etc/bob/runtime-payload.json' in image
    assert 'BOB_BUILD_ID=$version' in build and 'BOB_GIT_REV=' in build and 'BOB_BUILD_EPOCH=' in build
    assert "version['archive_sha256']" in build and '.release.json' in build


def test_upgrade_never_stops_storage_or_its_own_supervisor(modules):
    ops = modules[1].LinuxOperations()
    for unit in ('postgresql.service', 'redis-server.service', 'bob-qdrant.service',
                 'bob-firstboot.service', 'bob-runtime-upgrade.service'):
        assert not ops.managed_unit(unit)
    for unit in ('bob-core@owner.service', 'bob-worker@abc.socket', 'bob-token-renew.timer'):
        assert ops.managed_unit(unit)


def test_manifest_refuses_symlinks_to_user_data_and_link_parents(tmp_path, modules):
    payload, _ = modules
    _, _, original = package(tmp_path, modules)
    for bad_row in (dict(path='/opt/bob/code.py', kind='symlink', target='/var/lib/bob/family/owner/lane.env', mode=0o777),
                    dict(path='/opt/bob', kind='symlink', target='/opt/python/interpreter', mode=0o777)):
        manifest = copy.deepcopy(original)
        manifest['entries'] = [bad_row if r['path'] == bad_row['path'] else r for r in manifest['entries']]
        body = dict(manifest)
        body.pop('payload_sha256')
        manifest['payload_sha256'] = hashlib.sha256(payload.canonical(body)).hexdigest()
        version = {k: manifest[k] for k in ('build_id', 'build_epoch', 'git_rev', 'payload_sha256')}
        with pytest.raises(ValueError):
            payload.validate(manifest, version)


def test_real_dump_restore_adapters_use_private_streams_and_recreate_database(tmp_path, modules, monkeypatch):
    _, upgrade = modules
    calls = []
    def command(args, **kwargs):
        calls.append(args)
        assert 'secret' not in str(args)
        if 'pg_dump' in args:
            kwargs['stdout'].write(b'fake custom archive')
        if 'pg_restore' in args:
            assert kwargs['stdin'].read() == b'fake custom archive'
        return SimpleNamespace(stdout=b'')
    monkeypatch.setattr(upgrade.subprocess, 'run', command)
    sql = []
    monkeypatch.setitem(sys.modules, 'firstboot', SimpleNamespace(psql=lambda text: sql.append(text)))
    ops = upgrade.LinuxOperations()
    ops.dump(tmp_path)
    assert (tmp_path / 'bob.dump').read_bytes() == b'fake custom archive'
    ops.restore_db(tmp_path)
    assert sql == ['DROP DATABASE IF EXISTS bobclaw WITH (FORCE);',
                   'CREATE DATABASE bobclaw OWNER bobclaw TEMPLATE template0;']
    assert '--single-transaction' in calls[-1] and '--exit-on-error' in calls[-1]


@pytest.mark.parametrize('phase', ['health', 'publish'])
def test_resume_restarts_units_and_rechecks_health_before_version_publication(tmp_path, modules, installed, phase):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    folder, state = engine.prepare(archive, digest, False)
    ops.preflight(folder, state)
    engine.backup(folder, state)
    engine.swap(folder, state)
    state.update(phase=phase, quiesced=True, migrations_started=True)
    engine.save(folder, state)
    ops.calls.clear()
    ops.db = 'new-db'
    result = engine.run(archive, digest)
    assert result['upgrade']['outcome'] == 'completed'
    assert ops.calls.index('stop') < ops.calls.index('start') < ops.calls.index('health')
    assert ops.active
    assert (folder / 'runner/runtime_upgrade.py').exists()


def test_c5_hooks_provision_egress_once_preserving_other_identities_and_lane_secrets(tmp_path, monkeypatch):
    accounts, commands = {}, []
    def getpwnam(name):
        if name not in accounts:
            raise KeyError(name)
        return accounts[name]
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=getpwnam))
    spec = importlib.util.spec_from_file_location('upgrade_hooks_test', LIB / 'upgrade_hooks.py')
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    file(tmp_path, 'etc/systemd/system/bob-egress-proxy@.service', '[Service]')
    env = file(tmp_path, 'var/lib/bob/family/owner/bob-memory/lane.env', 'SECRET=unchanged')
    ids = dict(lane_id='abc123', phase='ready', worker_uid=61001, service_uid=61000,
               gateway_uid=61002, bridge_uid=61003)
    identity = file(tmp_path, 'etc/bob/lane-identities/owner.json', json.dumps(ids))
    config = file(tmp_path, 'etc/bob/workers/abc123.json', json.dumps({**ids, 'profiles': {'python': {'network': False}}}))
    def write(path, text, *_):
        path.write_text(text)
    monkeypatch.setitem(sys.modules, 'common', SimpleNamespace(private_replace=write,
                        root_directory=lambda path: path.mkdir(parents=True, exist_ok=True)))
    worker = SimpleNamespace(CONFIG=config.parent, IDENTITIES=identity.parent,
                             SYSTEMD=tmp_path / 'etc/systemd/system',
                             load_identity=lambda name: json.loads(identity.read_text()), unused_uid=lambda: 61004,
                             nft_rules=lambda c: f"chain output {{ policy accept; }} chain worker {{ ip daddr 127.0.0.1 tcp dport {c['egress_port']} accept\nreject }}")
    monkeypatch.setitem(sys.modules, 'worker', worker)
    monkeypatch.setitem(sys.modules, 'lane', SimpleNamespace(lanes=lambda: [('owner', env)]))
    monkeypatch.setattr(hooks, 'Path', lambda value: tmp_path / Path(value).relative_to('/'))
    def run(args, **kwargs):
        commands.append(args)
        assert args[0] == 'useradd'
        accounts[args[-1]] = SimpleNamespace(pw_uid=61004, pw_gid=61004, pw_gecos='Bob isolated abc123 egress')
    monkeypatch.setattr(hooks.subprocess, 'run', run)
    for _ in range(2):
        hooks.c5_builder()
        assert hooks.c5_egress() == ['bob-egress-proxy@abc123.service', 'bob-worker-policy@abc123.service']
    for relative in ('bob-egress-proxy@abc123.service.d/identity.conf',
                     'bob-core@owner.service.d/egress.conf'):
        path = worker.SYSTEMD / relative
        assert path.stat().st_mode & 0o444 == 0o444
        assert 'SECRET' not in path.read_text()
    assert len(commands) == 1
    updated = json.loads(identity.read_text())
    assert all(updated[k] == v for k, v in ids.items())
    assert updated['egress_uid'] == 61004
    updated = json.loads(config.read_text())
    assert updated['egress_port'] == 21004
    assert updated['profiles']['builder']['egress'] == 'lane-proxy/1'
    assert updated['profiles']['python'] == {'network': False}
    assert env.read_text() == 'SECRET=unchanged'
    (env.parent / '.disabled').touch()
    assert hooks.c5_egress() == []


def test_packaged_memory_slot_default_does_not_reset_user_embedder_selection(tmp_path, modules, installed):
    _, upgrade = modules
    file(installed, 'opt/bob/deploy/runtime/config/memory_slots.toml', '# old default')
    selected = file(installed, 'etc/bob/memory_slots.toml', '# user selected 0.6B / 1024 dimensions')
    archive, digest, _ = package(tmp_path, modules)
    result = upgrade.Upgrade(installed, FakeOperations(installed)).run(archive, digest)
    assert result['upgrade']['outcome'] == 'completed'
    assert selected.read_text() == '# user selected 0.6B / 1024 dimensions'


def test_root_cli_status_resume_and_rollback_use_the_existing_journal(tmp_path, modules, installed, monkeypatch, capsys):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    folder, state = engine.prepare(archive, digest, False)
    ops.preflight(folder, state)
    engine.backup(folder, state)
    engine.swap(folder, state)
    state.update(phase='health', quiesced=True, migrations_started=True)
    engine.save(folder, state)
    monkeypatch.setattr(upgrade, 'Upgrade', lambda: engine)
    monkeypatch.setattr(upgrade.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(upgrade.os, 'umask', lambda value: 0)
    monkeypatch.setattr(upgrade, 'open', lambda *a: nullcontext(None), raising=False)
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(flock=lambda *a: None, LOCK_EX=1, LOCK_NB=2))
    capsys.readouterr()
    assert upgrade.main(['--status']) == 0
    assert json.loads(capsys.readouterr().out)['upgrade']['phase'] == 'health'
    assert upgrade.main(['--resume', state['id']]) == 0
    assert engine.status()['upgrade']['outcome'] == 'completed'
    assert upgrade.main(['--rollback', state['id']]) == 0
    assert engine.status()['upgrade']['outcome'] == 'rolled_back'
    ops.failure = 'migrate'
    assert upgrade.main(['--payload', str(archive), '--expected-sha256', digest]) == 1
    assert engine.status()['upgrade']['outcome'] == 'rolled_back'


def test_manual_rollback_before_quiescence_does_not_stop_live_units(tmp_path, modules, installed):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    folder, state = engine.prepare(archive, digest, False)
    engine.rollback(folder, state)
    assert ops.calls == [] and ops.active
    assert engine.status()['upgrade']['outcome'] == 'rolled_back'


def test_persistent_systemd_gates_are_idempotent_and_removed_only_for_activation(tmp_path, modules, installed, monkeypatch):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    engine = upgrade.Upgrade(installed, FakeOperations(installed))
    folder, state = engine.prepare(archive, digest, False)
    state['units'] = ['bob-core@owner.service', 'bob-worker@abc.socket']
    monkeypatch.setitem(sys.modules, 'common', SimpleNamespace(
        root_directory=lambda path: path.mkdir(parents=True, exist_ok=True)))
    monkeypatch.setattr(upgrade, 'Path', lambda value: installed / Path(value).relative_to('/'))
    ops = upgrade.LinuxOperations()
    reloads = []
    ops.reload = lambda: reloads.append(True)
    ops.block(folder, state, lambda: None)
    ops.block(folder, state, lambda: None)
    assert (engine.store / 'pending').read_text().strip() == state['id']
    assert state['gates'].count('bob-core@owner.service') == 1
    guard = installed / 'etc/systemd/system/bob-core@owner.service.d/bob-runtime-upgrade.conf'
    assert guard.read_text() == '[Unit]\nConditionPathExists=!/var/lib/bob-upgrades/pending\n'
    assert 'bob-core@.service' in state['gates']
    assert len(reloads) == 2
    ops.unblock(state)
    assert not guard.exists() and not (engine.store / 'pending').exists()


class RecordingSystemctl:
    """Offline adapter oracle: reject socket collisions and missing templates."""
    def __init__(self, upgrade):
        self.upgrade = upgrade
        self.calls, self.active, self.triggers = [], set(), {}
        self.status, self.missing = {}, set()
        self.start_error = False
        self.disable_error = False

    def __call__(self, args, **kwargs):
        assert args[0] == 'systemctl'
        self.calls.append(args)
        action = args[1]
        if action == 'list-units':
            names = self.active | set(self.triggers)
            return SimpleNamespace(stdout=json.dumps([
                dict(unit=u, active='active' if u in self.active else 'inactive')
                for u in sorted(names)]).encode())
        if action == 'show':
            unit = args[2]
            if '--property=LoadState' in args:
                return SimpleNamespace(stdout=b'not-found\n' if unit in self.missing else b'loaded\n')
            if '--property=TriggeredBy' in args:
                return SimpleNamespace(stdout=' '.join(self.triggers.get(unit, [])).encode())
            return SimpleNamespace(stdout=self.status.get(unit,
                'ActiveState=active\nSubState=running\nType=simple\nResult=success\n').encode())
        if action in ('stop', 'disable'):
            if any(u in self.missing for u in args[2:]) or (action == 'disable' and self.disable_error):
                raise self.upgrade.subprocess.CalledProcessError(1, args)
            if action == 'stop':
                self.active.difference_update(args[2:])
        if action == 'start':
            for service, sockets in self.triggers.items():
                assert not (service in args and any(s in args for s in sockets)), 'socket collision'
            if self.start_error:
                raise self.upgrade.subprocess.CalledProcessError(1, args)
        return SimpleNamespace(stdout=b'')


def test_stop_persists_actual_active_set_before_stopping_and_never_recaptures(modules):
    _, upgrade = modules
    ops = upgrade.LinuxOperations()
    ctl = RecordingSystemctl(upgrade)
    ops.command = ctl
    service, socket = 'bob-settings-control@abc.service', 'bob-settings-control@abc.socket'
    ctl.active = {service, socket, 'postgresql.service'}
    ctl.triggers[service] = [socket]
    state = dict(phase='stop', units=['bob-core@old.service'])  # Changed since preflight.
    receipts = []
    def persist():
        assert not any(c[1] == 'stop' for c in ctl.calls)
        receipts.append(copy.deepcopy(state))
    ops.record_active(state, persist)
    ops.stop(state)
    assert receipts[0]['units'] == sorted([service, socket])
    assert receipts[0]['socket_activators'] == {service: [socket]}
    assert ctl.calls[-1] == ['systemctl', 'stop', service, socket]
    ctl.active = {'bob-egress-proxy@new.service'}
    ops.record_active(state, lambda: pytest.fail('must not overwrite original active receipt'))
    ops.stop(state)
    assert state['units'] == sorted([service, socket])
    assert ctl.calls[-1] == ['systemctl', 'stop', 'bob-egress-proxy@new.service']


@pytest.mark.parametrize('phase', ['start', 'rollback'])
def test_start_restores_socket_activation_standalone_services_and_required_new_units(modules, phase):
    _, upgrade = modules
    ops = upgrade.LinuxOperations()
    ctl = RecordingSystemctl(upgrade)
    ops.command = ctl
    # Include an Accept=yes connection instance and an explicitly named service.
    ctl.triggers = {f'bob-{kind}@abc.service': [f'bob-{kind}@abc.socket']
                    for kind in ('settings-control', 'worker-journal', 'worker')}
    ctl.triggers['bob-receipt@connection.service'] = ['bob-settings-receipt@abc.socket']
    units = list(ctl.triggers) + [s for sockets in ctl.triggers.values() for s in sockets]
    units += ['bob-core@owner.service', 'bob-credentials@owner.service']
    ctl.status['bob-credentials@owner.service'] = (
        'ActiveState=active\nSubState=exited\nType=oneshot\nResult=success\n')
    state = dict(phase=phase, units=units, start_new=['bob-egress-proxy@abc.service'])
    saved = []
    ops.start(state, lambda: saved.append(copy.deepcopy(state)))
    starts = [c for c in ctl.calls if c[1] == 'start']
    assert len(starts) == 1  # systemd orders the complete dependency transaction.
    selected = set(starts[0][2:])
    assert selected == (set(units) - set(ctl.triggers)) | (
        {'bob-egress-proxy@abc.service'} if phase == 'start' else set())
    assert 'bob-gateway@inactive.service' not in selected
    assert saved[0]['unit_start_results'] == state['unit_start_results']
    assert all(r['ok'] for r in state['unit_start_results'])
    assert {r['unit'] for r in state['unit_start_results']} == selected


def test_start_uses_recorded_socket_relationship_when_live_trigger_is_gone(modules):
    _, upgrade = modules
    ops = upgrade.LinuxOperations()
    ctl = RecordingSystemctl(upgrade)
    ops.command = ctl
    state = dict(phase='rollback', units=['bob-receipt@old-connection.service'], start_new=[],
                 socket_activators={'bob-receipt@old-connection.service': ['bob-settings-receipt@abc.socket']})
    ops.start(state)
    assert ['systemctl', 'start', 'bob-settings-receipt@abc.socket'] in ctl.calls


@pytest.mark.parametrize('active,sub', [('active', 'exited'), ('inactive', 'dead')])
def test_start_tolerates_successful_oneshot_and_already_active_exited_on_error(modules, active, sub):
    _, upgrade = modules
    ops = upgrade.LinuxOperations()
    ctl = RecordingSystemctl(upgrade)
    ctl.start_error = active == 'active'
    ops.command = ctl
    unit = 'bob-credentials@owner.service'
    ctl.status[unit] = f'ActiveState={active}\nSubState={sub}\nType=oneshot\nResult=success\n'
    state = dict(phase='start', units=[unit], start_new=[])
    ops.start(state)
    assert state['unit_start_results'][0]['ok']


@pytest.mark.parametrize('start_error', [False, True])
def test_start_reports_each_unit_and_keeps_real_service_failure_fatal(modules, start_error):
    _, upgrade = modules
    ops = upgrade.LinuxOperations()
    ctl = RecordingSystemctl(upgrade)
    ops.command = ctl
    ctl.start_error = start_error
    ctl.status['bob-core@owner.service'] = 'ActiveState=failed\nSubState=failed\nType=simple\nResult=exit-code\n'
    state = dict(phase='start', units=['bob-core@owner.service', 'bob-credentials@owner.service'], start_new=[])
    receipts = []
    with pytest.raises(ValueError, match='activation failed|inactive after start'):
        ops.start(state, lambda: receipts.append(copy.deepcopy(state)))
    assert len(receipts[0]['unit_start_results']) == 2
    assert [r['ok'] for r in state['unit_start_results']] == [False, True]


@pytest.mark.parametrize('missing', [False, True])
def test_rollback_stops_disables_new_units_before_removing_template_and_reloads_after_restore(
        tmp_path, modules, installed, monkeypatch, missing):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    folder, state = engine.prepare(archive, digest, False)
    engine.backup(folder, state)
    template = '/etc/systemd/system/bob-egress-proxy@.service'
    state['paths'].append(template)
    state['existed'][template] = False
    state.update(phase='start', quiesced=True, enabled=['bob-egress-proxy@abc.service'])
    target = file(installed, template.lstrip('/'), '[Service]\n')
    ctl = RecordingSystemctl(upgrade)
    if missing:
        # Reproduce run4: an older rollback removed the C5-only template first.
        target.unlink()
        state.update(phase='rollback', outcome='rollback_failed')
        ctl.missing.add(state['enabled'][0])
    linux = upgrade.LinuxOperations()
    linux.command = ctl
    def deactivate(state):
        assert target.exists() is (not missing)
        linux.deactivate_new(state)
        ops.event('deactivate-new')
    ops.deactivate_new = deactivate
    def restore_controls(folder, state):
        assert not target.exists()
        assert ops.calls[-1] == 'reload'
        ops.event('restore-controls')
    ops.restore_controls = restore_controls
    engine.save(folder, state)
    monkeypatch.setattr(upgrade, 'Upgrade', lambda: engine)
    monkeypatch.setattr(upgrade.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(upgrade.os, 'umask', lambda value: 0)
    monkeypatch.setattr(upgrade, 'open', lambda *a: nullcontext(None), raising=False)
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace(flock=lambda *a: None, LOCK_EX=1, LOCK_NB=2))
    assert upgrade.main(['--rollback', state['id']]) == 0
    assert engine.status()['upgrade']['outcome'] == 'rolled_back'
    assert [c[1] for c in ctl.calls if c[1] in ('stop', 'disable')] == ['stop', 'disable']
    calls = ops.calls.copy()
    assert upgrade.main(['--rollback', state['id']]) == 0
    assert ops.calls == calls  # A terminal rollback is idempotent.


def test_new_unit_cleanup_does_not_hide_disable_errors_on_loaded_units(modules):
    _, upgrade = modules
    ops = upgrade.LinuxOperations()
    ctl = RecordingSystemctl(upgrade)
    ctl.disable_error = True
    ops.command = ctl
    with pytest.raises(upgrade.subprocess.CalledProcessError):
        ops.deactivate_new(dict(enabled=['bob-egress-proxy@abc.service']))


def test_stop_resume_after_gated_boot_preserves_original_active_set(tmp_path, modules, installed):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    folder, state = engine.prepare(archive, digest, False)
    ops.preflight(folder, state)
    engine.backup(folder, state)
    original = state['units'].copy()
    state.update(phase='stop', quiesced=True, active_units_recorded=True)
    engine.save(folder, state)
    ops.record_active = lambda *a: pytest.fail('gated reboot must not replace the active set')
    result = engine.run(archive, digest)
    assert result['upgrade']['outcome'] == 'completed'
    assert json.loads((folder / 'state.json').read_text())['units'] == original


def test_active_snapshot_failure_does_not_quiesce_runtime(tmp_path, modules, installed):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed, 'record-active')
    result = upgrade.Upgrade(installed, ops).run(archive, digest)
    assert result['upgrade']['outcome'] == 'rolled_back'
    assert ops.active and 'stop' not in ops.calls and 'block' not in ops.calls


@pytest.mark.parametrize('load', [b'not-found\n', b'not-loaded\n'])
def test_missing_unit_cleanup_accepts_loadstate_even_when_show_returns_nonzero(modules, load):
    _, upgrade = modules
    ops = upgrade.LinuxOperations()
    def command(args, **kwargs):
        raise upgrade.subprocess.CalledProcessError(1, args, output=load if args[1] == 'show' else b'')
    ops.command = command
    ops.deactivate_new(dict(enabled=['bob-egress-proxy@abc.service']))


def test_start_records_failed_status_query_and_still_reports_other_units(modules):
    _, upgrade = modules
    ops = upgrade.LinuxOperations()
    ctl = RecordingSystemctl(upgrade)
    def command(args, **kwargs):
        if args[1:3] == ['show', 'bob-core@owner.service'] and '--property=ActiveState' in args:
            raise upgrade.subprocess.CalledProcessError(1, args, output=b'')
        return ctl(args, **kwargs)
    ops.command = command
    state = dict(phase='start', units=['bob-core@owner.service', 'bob-gateway@owner.service'], start_new=[])
    with pytest.raises(ValueError):
        ops.start(state)
    assert [r['ok'] for r in state['unit_start_results']] == [False, True]


@pytest.mark.parametrize('existing_binary', [False, True])
def test_cc1_claude_binary_is_upgraded_and_rolled_back(tmp_path, modules, installed, existing_binary):
    payload, upgrade = modules
    if existing_binary:
        file(installed, 'usr/local/bin/claude', 'fake-old-binary')
    def include_claude(manifest, source):
        binary = file(source, 'usr/local/bin/claude', 'fake-new-binary')
        binary.chmod(0o755)
        revised = payload.build(source, manifest['build_id'], manifest['git_rev'], manifest['build_epoch'])
        manifest.clear()
        manifest.update(revised)
    archive, digest, manifest = package(tmp_path, modules, mutate=include_claude)
    assert '/usr/local/bin/claude' in manifest['roots']
    engine = upgrade.Upgrade(installed, FakeOperations(installed))
    receipt = engine.run(archive, digest)
    assert receipt['upgrade']['outcome'] == 'completed'
    assert (installed / 'usr/local/bin/claude').read_text() == 'fake-new-binary'
    folder = engine.store / receipt['upgrade']['id']
    engine.rollback(folder, json.loads((folder / 'state.json').read_text()))
    assert ((installed / 'usr/local/bin/claude').read_text() == 'fake-old-binary'
            if existing_binary else not (installed / 'usr/local/bin/claude').exists())


@pytest.mark.parametrize('case,code', [
    ('boundary', 'runtime_too_old'),
    ('identity', 'runtime_too_old'),
    ('space', 'insufficient_space'),
    ('mounted', 'unsupported_layout'),
    ('python', 'unsupported_layout'),
    ('invalid-identity', None),
])
def test_preflight_refusal_receipts(tmp_path, modules, installed, monkeypatch, case, code):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    engine = upgrade.Upgrade(installed, FakeOperations(installed))
    folder, state = engine.prepare(archive, digest, False)
    boundary = installed / 'etc/bob/execution-boundary'
    if case != 'boundary':
        file(installed, 'etc/bob/execution-boundary', 'worker')
    if case == 'python':
        manifest_path = folder / 'payload' / upgrade.MANIFEST
        manifest = json.loads(manifest_path.read_text())
        manifest['entries'].append(dict(kind='symlink', target='/opt/python/missing/bin/python'))
        manifest_path.write_text(json.dumps(manifest))
    real_path = Path
    def mapped(value):
        if isinstance(value, str) and value.startswith(('/etc/', '/opt/')):
            return installed / value.lstrip('/')
        return real_path(value)
    monkeypatch.setattr(upgrade, 'Path', mapped)
    monkeypatch.setattr(Path, 'is_mount', lambda p: case == 'mounted' and p == installed / 'opt/bob')
    monkeypatch.setattr(upgrade.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0 if case == 'space' else 10**15))
    linux = upgrade.LinuxOperations()
    monkeypatch.setattr(linux, 'health', lambda: None)
    monkeypatch.setattr(linux, 'command', lambda *a, **k: SimpleNamespace(stdout=b'[]'))
    monkeypatch.setitem(sys.modules, 'firstboot', SimpleNamespace(psql=lambda sql: '1'))
    env = installed / 'var/lib/bob/family/owner/bob-memory/lane.env'
    monkeypatch.setitem(sys.modules, 'lane', SimpleNamespace(lanes=lambda: [('owner', env)]))
    identities = tmp_path / 'identities'
    identities.mkdir()
    if case == 'invalid-identity':
        (identities / 'owner.json').write_text('invalid')
    def load_identity(name):
        if case in ('identity', 'invalid-identity'):
            raise ValueError('synthetic secret identity context')
        return {}
    monkeypatch.setitem(sys.modules, 'worker', SimpleNamespace(load_identity=load_identity, IDENTITIES=identities))
    engine.ops.preflight = lambda folder, state: linux.preflight(folder, state)
    receipt = engine.run(archive, digest)
    assert receipt['upgrade']['outcome'] == 'rolled_back'
    saved = json.loads((folder / 'state.json').read_text())
    if code is None:
        assert 'failure_code' not in saved and 'failure_code' not in receipt['upgrade']
    else:
        assert saved['failure_code'] == receipt['upgrade']['failure_code'] == code
    assert 'secret' not in (folder / 'state.json').read_text()
    assert 'stop' not in engine.ops.calls
    assert (installed / 'opt/bob/code.py').read_text() == 'old-code'


@pytest.mark.parametrize('case,code', [('staging-space', 'insufficient_space'),
                                      ('root-layout', 'unsupported_layout')])
def test_prepare_refusal_publishes_terminal_status(tmp_path, modules, installed, monkeypatch, case, code):
    payload, upgrade = modules
    archive, digest, manifest = package(tmp_path, modules)
    if case == 'staging-space':
        monkeypatch.setattr(upgrade.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0))
    else:
        # Individually allowed roots from different releases now overlap.
        file(installed, payload.MANIFEST, json.dumps(dict(roots=['/opt/bob/nested'])))
    ops = FakeOperations(installed)
    engine = upgrade.Upgrade(installed, ops)
    with pytest.raises(ValueError):
        engine.run(archive, digest)
    receipt = engine.status()['upgrade']
    assert receipt['outcome'] == 'rolled_back' and receipt['failure_code'] == code
    saved_path = engine.store / receipt['id'] / 'state.json'
    saved = json.loads(saved_path.read_text())
    assert saved['failure_code'] == code and saved['failure_phase'] == 'preflight'
    assert not saved['quiesced'] and not ops.calls
    assert not list(engine.store.glob('staging-*'))
    assert engine.rollback(saved_path.parent, saved) == engine.status()


def test_health_refusal_code_survives_rollback_without_secret_text(tmp_path, modules, installed, monkeypatch):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    linux = upgrade.LinuxOperations()
    monkeypatch.setattr(linux, 'command', lambda *a, **k: SimpleNamespace(stdout=b'{"healthy":false}'))
    ticks = iter([0, 121])
    monkeypatch.setattr(upgrade.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(upgrade.UpgradeRefusal) as refused:
        linux.health()
    assert refused.value.failure_code == 'runtime_unhealthy'
    ops = FakeOperations(installed)
    health = ops.health
    calls = 0
    def fail_new_health():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise refused.value
        health()
    ops.health = fail_new_health
    engine = upgrade.Upgrade(installed, ops)
    receipt = engine.run(archive, digest)
    saved = json.loads((engine.store / receipt['upgrade']['id'] / 'state.json').read_text())
    assert receipt['upgrade']['failure_code'] == saved['failure_code'] == 'runtime_unhealthy'
    assert receipt['upgrade']['outcome'] == 'rolled_back'
    assert saved['failure_phase'] == 'health' and saved['failure'] == 'UpgradeRefusal'
    assert 'runtime health gate failed' not in json.dumps(saved)
    assert ops.db == 'old-db' and (installed / 'opt/bob/code.py').read_text() == 'old-code'


def test_unclassified_failure_has_no_code_even_with_matching_text(tmp_path, modules, installed):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    ops = FakeOperations(installed, 'preflight')
    engine = upgrade.Upgrade(installed, ops)
    receipt = engine.run(archive, digest)
    saved_path = engine.store / receipt['upgrade']['id'] / 'state.json'
    saved = json.loads(saved_path.read_text())
    assert saved['failure'] == 'ValueError'
    assert 'failure_code' not in saved and 'failure_code' not in receipt['upgrade']
    assert 'synthetic failure' not in saved_path.read_text() and 'secret' not in saved_path.read_text()
    state = {'phase': 'preflight', 'failure_code': 'runtime_too_old'}
    upgrade.record_failure(state, ValueError('runtime health gate failed; secret'))
    assert 'failure_code' not in state


def test_status_never_publishes_unknown_or_free_text_failure_codes(tmp_path, modules, installed):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    engine = upgrade.Upgrade(installed, FakeOperations(installed))
    folder, state = engine.prepare(archive, digest, False)
    state['failure_code'] = 'secret free text'
    engine.save(folder, state)
    assert 'failure_code' not in engine.status()['upgrade']
    with pytest.raises(ValueError, match='invalid upgrade refusal code'):
        upgrade.UpgradeRefusal('secret free text', 'unused')


def test_staging_refusal_keeps_previous_upgrade_rollback_available(tmp_path, modules, installed, monkeypatch):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    engine = upgrade.Upgrade(installed, FakeOperations(installed))
    completed = engine.run(archive, digest)['upgrade']
    previous = engine.store / completed['id']
    state = json.loads((previous / 'state.json').read_text())
    next_archive, next_digest, _ = package(tmp_path, modules, 'c6', 6)
    monkeypatch.setattr(upgrade.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0))
    with pytest.raises(ValueError):
        engine.run(next_archive, next_digest)
    assert engine.status()['upgrade']['failure_code'] == 'insufficient_space'
    assert (engine.store / 'latest').read_text().strip() == completed['id']
    result = engine.rollback(previous, state)
    assert result['upgrade']['id'] == completed['id']
    assert result['upgrade']['outcome'] == 'rolled_back'
    assert 'failure_code' not in result['upgrade']
    assert (installed / 'opt/bob/code.py').read_text() == 'old-code'


def test_stale_attempt_pointer_cannot_hide_a_new_active_upgrade(tmp_path, modules, installed, monkeypatch):
    _, upgrade = modules
    archive, digest, _ = package(tmp_path, modules)
    engine = upgrade.Upgrade(installed, FakeOperations(installed))
    with monkeypatch.context() as low_space:
        low_space.setattr(upgrade.shutil, 'disk_usage', lambda p: SimpleNamespace(free=0))
        with pytest.raises(ValueError):
            engine.run(archive, digest)
    old_pointer = (engine.store / 'latest-attempt').read_text()
    folder, state = engine.prepare(archive, digest, False)
    (engine.store / 'latest-attempt').write_text(old_pointer)
    receipt = engine.status()['upgrade']
    assert receipt['id'] == state['id'] and receipt['phase'] == 'preflight'
    assert 'failure_code' not in receipt
