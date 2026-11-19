"""Offline migration contracts. Kernel enforcement belongs to acceptance.py."""
from contextlib import contextmanager
import copy
import errno
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from types import SimpleNamespace

import pytest

LIB = Path(__file__).resolve().parents[1] / 'lib'
CORE = LIB.parents[2] / 'bobclaw-core'


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(CORE))
    if os.name == 'nt':
        monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=lambda n: SimpleNamespace(pw_uid=10, pw_gid=10)))
        monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace())
    modules = {}
    for name in ('common', 'worker', 'lane', 'migration_paths', 'migration'):
        spec = importlib.util.spec_from_file_location(name, LIB / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    m, common, worker, lane = [modules[n] for n in ('migration', 'common', 'worker', 'lane')]
    monkeypatch.setattr(common, 'DATA', tmp_path)
    monkeypatch.setattr(common, 'MIGRATIONS', tmp_path / 'worker-migrations')
    monkeypatch.setattr(m, 'DATA', tmp_path)
    monkeypatch.setattr(m, 'STATE', tmp_path / 'worker-migrations')
    monkeypatch.setattr(m, 'BOUNDARY', tmp_path / 'etc/execution-boundary')
    monkeypatch.setattr(m, 'SYSTEMD', tmp_path / 'systemd')
    monkeypatch.setattr(lane, 'FAMILY', tmp_path / 'family')
    monkeypatch.setattr(worker, 'IDENTITIES', tmp_path / 'identities')
    worker.IDENTITIES.mkdir()
    @contextmanager
    def directory(path, create=False):
        if create:
            path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ValueError('unsafe directory')
        yield path
    monkeypatch.setattr(m, 'directory', directory)
    monkeypatch.setattr(m, 'private_directory', lambda p: p.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(m, 'save', lambda p, v: p.write_text(json.dumps(v)))
    monkeypatch.setattr(m.os, 'fsync', lambda *a: None)
    def write(path, content, *args, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setattr(m, 'private_replace', write)
    monkeypatch.setattr(worker, 'private_replace', write)
    monkeypatch.setattr(worker.os, 'chmod', lambda *a, **k: None)
    monkeypatch.setattr(m, 'install_marker', lambda: write(m.BOUNDARY, 'worker\nisolation-version=1\n'))
    calls = []
    monkeypatch.setattr(m, 'systemctl', lambda *a: calls.append(a))
    monkeypatch.setattr(m, 'wait_ready', lambda values, timeout=60: calls.append(('wait-ready',)))
    monkeypatch.setattr(m, 'stop_workers', lambda ids: calls.append(('stop-workers', ids['lane_id'])))
    ids = dict(lane_id='abc123', service_user='bob-s-abc123', service_uid=1001, service_gid=1001,
               worker_user='bob-w-abc123', worker_uid=1002, worker_gid=1002)
    monkeypatch.setattr(worker, 'load_identity', lambda name: ids)
    monkeypatch.setattr(worker, 'identity', lambda name: ids)
    monkeypatch.setattr(worker, 'require_root', lambda: None)
    monkeypatch.setattr(worker, 'service_environments', lambda root: calls.append(('role-envs',)))
    monkeypatch.setattr(worker, 'provision', lambda *a, **k: calls.append(('provision', k)))
    native_admission = worker.set_admission
    def admission(ids, enabled):
        if enabled:
            assert m.state_for('alpha')['phase'] == 'completed'
            assert not m.pending('alpha')
        calls.append(('admission', enabled))
    monkeypatch.setattr(worker, 'set_admission', admission)
    root = lane.FAMILY / 'alpha/bob-memory'
    root.mkdir(parents=True)
    original = lane.lane_values('alpha', 7835, 7836, 8902, 'keep-password', 'keep-totp', 'old-authority')
    (root / 'lane.env').write_text(common.render_env(original))
    m.fixture = SimpleNamespace(modules=modules, root=root, ids=ids, calls=calls, original=original,
                                native_admission=native_admission)
    return m


def test_dry_run_has_no_journal_accounts_marker_or_service_effects(runtime):
    m = runtime
    assert m.migrate('alpha', True) == dict(lane='alpha', version=1, phase='quiesce')
    assert not m.STATE.exists() and not m.BOUNDARY.exists()
    assert not m.fixture.calls


def test_already_isolated_lane_requires_quiesced_settings_upgrade(runtime):
    m = runtime
    values = {**m.fixture.original, 'BOB_WORKER_ID': m.fixture.ids['lane_id']}
    (m.fixture.root / 'lane.env').write_text(m.render_env(values))
    # No journal/fresh v1 identity cannot silently turn into a settings-ready lane.
    assert m.plan('alpha')['phase'] == 'quiesce'
    path = m.STATE / 'alpha'
    path.mkdir(parents=True)
    (path / 'state.json').write_text(json.dumps(dict(lane='alpha',version=1,phase='completed',activation_complete=True)))
    plan = m.plan('alpha')
    assert plan['phase'] == 'quiesce' and plan['settings_upgrade'] is True
    assert not m.fixture.calls


def test_unknown_version_and_custom_external_path_refused(runtime):
    m = runtime
    root = m.fixture.root
    env = m.read_env(root / 'lane.env')
    env['CODEX_HOME'] = '/mnt/c/Users/host/.codex'
    (root / 'lane.env').write_text(m.render_env(env))
    with pytest.raises(ValueError, match='nonstandard CODEX_HOME'):
        m.plan('alpha')
    (root / 'lane.env').write_text(m.render_env(m.fixture.original))
    path = m.STATE / 'alpha'
    path.mkdir(parents=True)
    (path / 'state.json').write_text(json.dumps(dict(version=99, lane='alpha', phase='quiesce')))
    with pytest.raises(ValueError, match='unsupported migration journal'):
        m.migrate('alpha')


@pytest.mark.parametrize('key', ['PYTHONPATH', 'LD_PRELOAD', 'HOME', 'NODE_OPTIONS'])
def test_legacy_env_cannot_promote_worker_controlled_loader_settings(runtime, key):
    (runtime.fixture.root / 'providers.env').write_text(runtime.render_env({key: '/worker-controlled'}))
    with pytest.raises(ValueError, match='unsafe process-loader override'):
        runtime.migrate('alpha')
    assert not runtime.STATE.exists()


def fake_phases(m, monkeypatch):
    calls = []
    worker = m.fixture.modules['worker']
    monkeypatch.setattr(worker, 'CONFIG', m.fixture.root / 'worker-config')
    def provision(*args, **kwargs):
        m.fixture.calls.append(('provision', kwargs))
        worker.CONFIG.mkdir(exist_ok=True)
        profiles = {name: {'network': True, 'egress': 'lane-proxy/1'}
                    for name in ('builder', 'codex', 'claude', 'mcp')}
        (worker.CONFIG / (m.fixture.ids['lane_id'] + '.json')).write_text(json.dumps({'profiles': profiles}))
    monkeypatch.setattr(worker, 'provision', provision)
    def quiesce(name, root, state, persist):
        calls.append('quiesce')
        m.block(name)
        m.install_marker()
    def identities(name, root, state, persist):
        calls.append('identities')
        # Model the settings split and S1b/S5 egress/profile upgrade rather than
        # publishing an old identity that correctly requires another migration.
        m.fixture.ids.update(gateway_uid=1003, bridge_uid=1004, egress_uid=61004)
        state['identity'] = m.fixture.ids
    def ownership(name, root, state, persist):
        calls.append('ownership')
        state['ownership']['inventory_complete'] = True
        m.disable_id(root, m.fixture.ids)
    def credentials(name, root, state, persist):
        calls.append('credentials')
        state['rotation'] = {'portal_reissued': True}
    def verify(name, root, state, persist):
        calls.append('verify')
        assert m.read_env(root / 'lane.env')['BOB_WORKER_ID'].startswith('pending-')
        state['verification'] = {'canary_denied': True}
        persist()
    for phase, function in dict(quiesce=quiesce, controls=identities, ownership=ownership,
                                 credentials=credentials, verify=verify).items():
        monkeypatch.setattr(m, phase, function)
    return calls


@pytest.mark.parametrize('phase', ['controls', 'ownership', 'credentials', 'verify', 'publish'])
def test_every_phase_failure_disables_and_resumes_without_legacy_fallback(runtime, monkeypatch, phase):
    m = runtime
    calls = fake_phases(m, monkeypatch)
    operation = getattr(m, phase)
    def interrupt(*args):
        if phase == 'publish':
            # Fail after publication, exercising the completed+pending recovery.
            operation(*args)
        raise RuntimeError('simulated interruption')
    monkeypatch.setattr(m, phase, interrupt)
    with pytest.raises(RuntimeError):
        m.migrate('alpha')
    assert m.pending('alpha') and m.BOUNDARY.is_file()
    assert ('stop', *m.lane.units('alpha')) in m.fixture.calls
    # Before allocation there is no ID; afterward it must be deliberately unusable.
    assert m.read_env(m.fixture.root / 'lane.env').get('BOB_WORKER_ID') != m.fixture.ids['lane_id']
    monkeypatch.setattr(m, phase, operation)
    assert m.migrate('alpha')['phase'] == 'completed'
    assert not m.pending('alpha')
    assert calls.count('quiesce') == 2
    before = (m.STATE / 'alpha/state.json').read_bytes()
    call_count = len(m.fixture.calls)
    assert m.migrate('alpha')['phase'] == 'completed'
    assert (m.STATE / 'alpha/state.json').read_bytes() == before
    assert len(m.fixture.calls) == call_count


def test_durable_gate_blocks_every_operator_entrypoint(runtime):
    m = runtime
    m.block('alpha')
    common = m.fixture.modules['common']
    with pytest.raises(ValueError, match='migration incomplete'):
        common.ensure_codex_home(m.fixture.root)
    with pytest.raises(ValueError, match='migration incomplete'):
        common.writer_guard('alpha')
    for unit in m.lane.units('alpha'):
        text = (m.SYSTEMD / (unit + '.d/migration.conf')).read_text()
        assert 'ConditionPathExists=!' + str(m.STATE / 'alpha/pending') in text


def test_credential_rotation_replay_keeps_password_totp_provider_and_new_keys(runtime, monkeypatch):
    m = runtime
    root = m.fixture.root
    (root / 'providers.env').write_bytes(b'DEEPSEEK_API_KEY="synthetic"\n')
    state = {}
    receipts = []
    tokens = []
    def mint(name):
        values = m.read_env(root / 'lane.env')
        tokens.append(values['JWT_SECRET'])
        if len(tokens) == 1:
            raise RuntimeError('power loss after trusted mint')
    monkeypatch.setattr(m.lane, 'rotate_token', mint)
    with pytest.raises(RuntimeError):
        m.credentials('alpha', root, state, lambda: receipts.append(copy.deepcopy(state)))
    first = m.read_env(root / 'lane.env')
    m.credentials('alpha', root, state, lambda: receipts.append(copy.deepcopy(state)))
    after = m.read_env(root / 'lane.env')
    assert after == first
    assert after['JWT_SECRET'] != after['BOBCLAW_SECRET'] != 'old-authority'
    assert after['BOBCLAW_PASSWORD'] == 'keep-password' and after['TOTP_SECRET'] == 'keep-totp'
    assert tokens == [after['JWT_SECRET']] * 2
    assert state['rotation']['portal_reissued']
    assert receipts[0]['rotation']['keys_published'] is False
    assert (root / 'providers.env').read_bytes() == b'DEEPSEEK_API_KEY="synthetic"\n'


def test_publish_requires_verification_and_portal_receipt(runtime):
    with pytest.raises(ValueError, match='cannot enable execution'):
        runtime.publish('alpha', runtime.fixture.root, {}, lambda: None)


def test_public_config_disables_core_submission_without_changing_worker_authority(runtime, monkeypatch, tmp_path):
    m = runtime
    worker = m.fixture.modules['worker']
    folder = tmp_path / 'workers'
    folder.mkdir()
    monkeypatch.setattr(worker, 'CONFIG', folder)
    path = folder / (m.fixture.ids['lane_id'] + '.json')
    config = {**m.fixture.ids, 'profiles': {'python': {'argv': ['/python'], 'network': False}},
              'workspaces': {'scratch': '/scratch'}, 'socket': '/exec.sock', 'auth_socket': '/auth.sock'}
    path.write_text(json.dumps(config))
    m.fixture.native_admission(m.fixture.ids, False)
    disabled = json.loads(path.read_text())
    assert disabled['socket'].endswith('/migration-disabled')
    assert disabled['auth_socket'].endswith('/migration-disabled-auth')
    assert disabled['profiles'] == config['profiles'] and disabled['workspaces'] == config['workspaces']
    assert disabled['worker_uid'] == config['worker_uid'] and disabled['service_uid'] == config['service_uid']
    from core.worker import client, contract
    from core import lane_config
    monkeypatch.setattr(lane_config, 'get_lane', lambda: SimpleNamespace(values={'BOB_WORKER_ID': m.fixture.ids['lane_id']}))
    monkeypatch.setattr(contract, 'root_json', lambda p: disabled)
    assert client.unavailable_reason().startswith('unavailable:')
    m.fixture.native_admission(m.fixture.ids, True)
    enabled = json.loads(path.read_text())
    assert enabled['socket'].endswith('/exec.sock') and enabled['auth_socket'].endswith('/auth.sock')
    assert enabled['admission'] == 'enabled'


def test_quiesce_stops_all_shared_uid_peers_and_journals_before_freezing(runtime, monkeypatch):
    m = runtime
    other = m.lane.FAMILY / 'beta/bob-memory'
    other.mkdir(parents=True)
    (other / 'lane.env').write_text('BOB_DEFAULT_OWNER="beta"\n')
    killed = []
    monkeypatch.setattr(m, 'kill_uid', lambda uid: killed.append(uid))
    monkeypatch.setattr(m, 'install_units', lambda: None)
    monkeypatch.setattr(m.pwd, 'getpwnam', lambda n: SimpleNamespace(pw_uid=10, pw_gid=10))
    native_fstat = m.os.fstat
    monkeypatch.setattr(m.os, 'fstat', lambda path: path.stat() if isinstance(path, Path) else native_fstat(path))
    state, saved = {'ownership': {}, 'helpers': {}}, []
    def freeze(fd, uid, gid, mode):
        if fd in (m.DATA, m.lane.FAMILY):
            assert saved[-1]['shared_ancestors'][str(fd)] == m.describe(fd.stat())
            assert (uid, gid, mode) == (0, 0, 0o711)
        else:
            assert saved[-1]['ancestor'] == m.describe(fd.stat())
            assert (uid, gid, mode) == (0, 0, 0o700)
    monkeypatch.setattr(m, 'apply', freeze)
    m.quiesce('alpha', m.fixture.root, state, lambda: saved.append(copy.deepcopy(state)))
    assert m.pending('alpha') and m.pending('beta')
    for name in ('alpha', 'beta'):
        assert ('stop', *m.lane.units(name)) in m.fixture.calls
    assert ('stop', 'bob-token-renew.timer', 'bob-token-renew.service') in m.fixture.calls
    assert len(killed) == 1


def test_service_recovery_keeps_execution_off_and_retains_version(runtime, monkeypatch):
    m = runtime
    fake_phases(m, monkeypatch)
    m.migrate('alpha')
    m.suspend('alpha')
    state = m.state_for('alpha')
    assert state['phase'] == 'verify' and state['version'] == 1 and state['suspended']
    assert m.pending('alpha') and m.BOUNDARY.exists()
    assert m.read_env(m.fixture.root / 'lane.env')['BOB_WORKER_ID'].startswith('pending-')
    assert m.migrate('alpha')['phase'] == 'completed'
    assert not m.state_for('alpha').get('suspended')


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'mount', 'fifo'])
def test_unsafe_inode_refusals_on_windows_too(runtime, kind):
    module = runtime.fixture.modules['migration_paths']
    info = SimpleNamespace(st_dev=5 if kind == 'mount' else 1, st_nlink=2 if kind == 'hardlink' else 1,
                           st_mode={'symlink': stat.S_IFLNK, 'hardlink': stat.S_IFREG,
                                    'mount': stat.S_IFDIR, 'fifo': stat.S_IFIFO}[kind])
    with pytest.raises(ValueError):
        module.check_inode(info, 1)


def test_selective_ownership_preserves_only_intended_grants(runtime):
    module = runtime.fixture.modules['migration_paths']
    ids = runtime.fixture.ids
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o6777)
    assert module.ownership(('gateway.db',), info, ids) == (1001, 1001, 0o700)
    assert module.ownership(('codex-home', 'auth.json'), info, ids) == (1002, 1002, 0o660)
    assert module.ownership(('lane.env',), info, ids) == (0, 1001, 0o640)
    assert module.ownership(('projects', 'x'), info, ids) == (1002, 1001, 0o770)
    assert module.ownership(('cli', 'settings'), info, ids) == (1002, 1002, 0o700)


def test_transfer_replay_retains_original_ownership_after_partial_chown(runtime, monkeypatch):
    module = runtime.fixture.modules['migration_paths']
    infos = {(): SimpleNamespace(st_dev=1, st_ino=10, st_uid=9, st_gid=9,
                                 st_mode=stat.S_IFDIR | 0o700, st_nlink=2),
             ('secret',): SimpleNamespace(st_dev=1, st_ino=11, st_uid=9, st_gid=9,
                                         st_mode=stat.S_IFREG | 0o600, st_nlink=1)}
    def walk(path, visit, **kwargs):
        for rel, info in infos.items():
            visit(rel, rel, info, None, None)
    monkeypatch.setattr(module, 'walk', walk)
    changed = []
    def apply(fd, uid, gid, mode):
        changed.append(fd)
        infos[fd].st_uid, infos[fd].st_gid = uid, gid
        if len(changed) == 1:
            raise RuntimeError('power loss after first chown')
    monkeypatch.setattr(module, 'apply', apply)
    manifest, receipts = {}, []
    save = lambda: receipts.append(copy.deepcopy(manifest))
    with pytest.raises(RuntimeError):
        module.transfer(Path('/unused'), runtime.fixture.ids, manifest, save)
    assert receipts[0]['entries']['']['uid'] == 9
    module.transfer(Path('/unused'), runtime.fixture.ids, manifest, save)
    assert manifest['entries']['']['uid'] == 9 and manifest['applied']
    assert infos[('secret',)].st_uid == 1001


@pytest.mark.skipif(os.name == 'nt', reason='real dirfd/O_NOFOLLOW enforcement needs Linux')
@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'mount', 'bind-mount'])
def test_real_walk_refuses_unsafe_tree_without_mutating(runtime, monkeypatch, tmp_path, kind):
    module = runtime.fixture.modules['migration_paths']
    # Restore the production directory implementation (the fixture only stubs
    # migration orchestration, not this module's walker).
    root = tmp_path / 'tree'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_text('unchanged')
    if kind == 'symlink':
        (root / 'link').symlink_to(outside)
    elif kind == 'hardlink':
        os.link(outside, root / 'link')
    else:
        monkeypatch.setattr(module, 'mount_points', lambda: {str(root if kind == 'mount' else root / 'bind')})
    with pytest.raises((ValueError, OSError)):
        module.transfer(root, runtime.fixture.ids, {}, lambda: None)
    assert outside.read_text() == 'unchanged'


def test_embedded_selfcheck_and_acceptance_parse(runtime):
    import ast
    selfcheck = ast.parse(runtime.SELF_CHECK)
    code = next(node.value for node in ast.walk(selfcheck) if isinstance(node, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'code' for t in node.targets))
    ast.parse(eval(compile(ast.Expression(code), '<self-check>', 'eval'),
                   {'c': {'canary': '/tmp/synthetic', 'worker_gid': 1002}}))
    tree = ast.parse((LIB.parent / 'tests/acceptance.py').read_text())
    functions = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert {'legacy_migration_fixture', 'migrate_existing', 'run'} <= functions
    settings = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'settings_acceptance')
    programs = 0
    for node in ast.walk(settings):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {'source','code','read_source'} for t in node.targets):
            program = eval(compile(ast.Expression(node.value), '<settings-probe>', 'eval'),
                           {'vault': Path('/tmp/synthetic-vault'), 'ids': {'lane_id': 'a'*12}})
            ast.parse(program)
            programs += 1
    assert programs == 3


def test_firstboot_hook_runs_after_completion_and_does_not_block_on_lane_dependencies():
    wrapper = (LIB.parent / 'bin/bob-firstboot').read_text()
    assert '--migrate-workers' in wrapper and "'--no-block'" in wrapper
    unit = (LIB.parent / 'systemd/bob-migrate-workers.service').read_text()
    assert 'After=bob-firstboot.service' in unit
    assert 'bob-runtime migrate-workers' in unit


@pytest.fixture
def tree(runtime, monkeypatch):
    """In-memory syscall boundary: exercises the real walker on Windows too.

    Inodes can have multiple names and descriptors pin inodes across renames.
    Ownership/ACL calls are audited independently of directory entries. This is
    not a kernel oracle; real descriptor tests below and acceptance cover Linux.
    """
    module = runtime.fixture.modules['migration_paths']
    class Tree:
        def __init__(self):
            self.entries, self.fds, self.calls, self.opens = {}, {}, [], []
            self.nextino = 100
            self.add((), stat.S_IFDIR | 0o700)

        def add(self, path, mode, data=b'', dev=1):
            key = tuple(path.split('/')) if isinstance(path, str) else path
            self.nextino += 1
            node = SimpleNamespace(st_dev=dev, st_ino=self.nextino, st_uid=9, st_gid=9,
                                   st_mode=mode, st_nlink=1, data=bytearray(data))
            self.entries[key] = node
            return node

        def link(self, source, dest):
            node = self.entries[tuple(source.split('/'))]
            node.st_nlink += 1
            self.entries[tuple(dest.split('/'))] = node

        def descriptor(self, key):
            fd = max(self.fds, default=20) + 1
            self.fds[fd] = [self.entries[key], key, 0]
            return fd

        def key(self, name, dir_fd):
            return self.fds[dir_fd][1] + (name,)

        def stat(self, name, *, dir_fd, follow_symlinks):
            assert follow_symlinks is False
            return copy.copy(self.entries[self.key(name, dir_fd)])

        def fstat(self, fd):
            return copy.copy(self.fds[fd][0])

        def listdir(self, fd):
            key = self.fds[fd][1]
            return [p[-1] for p in self.entries if len(p) == len(key) + 1 and p[:-1] == key]

        def open(self, name, flags, mode=0o777, *, dir_fd):
            key = self.key(name, dir_fd)
            if flags & os.O_CREAT:
                assert flags & os.O_EXCL
                if key in self.entries:
                    raise FileExistsError
                node = self.add(key, stat.S_IFREG | mode)
                node.st_uid = node.st_gid = 0
            node = self.entries[key]
            assert stat.S_ISREG(node.st_mode) or stat.S_ISDIR(node.st_mode), 'special entry opened'
            self.opens.append(key)
            return self.descriptor(key)

        def read(self, fd, count):
            node, key, position = self.fds[fd]
            value = bytes(node.data[position:position+count])
            self.fds[fd][2] += len(value)
            return value

        def write(self, fd, value):
            node, key, position = self.fds[fd]
            # Short writes must not truncate the copy.
            value = bytes(value[:7])
            node.data[position:position+len(value)] = value
            self.fds[fd][2] += len(value)
            return len(value)

        def replace(self, source, dest, *, src_dir_fd, dst_dir_fd):
            old, new = self.key(source, src_dir_fd), self.key(dest, dst_dir_fd)
            self.entries[new].st_nlink -= 1
            self.entries[new] = self.entries.pop(old)
            self.calls.append(('replace', new))

        def fchown(self, fd, uid, gid):
            node = self.fds[fd][0]
            self.calls.append(('chown', node.st_ino))
            node.st_uid, node.st_gid = uid, gid

        def fchmod(self, fd, mode):
            node = self.fds[fd][0]
            self.calls.append(('chmod', node.st_ino))
            node.st_mode = stat.S_IFMT(node.st_mode) | mode

        def chown(self, name, uid, gid, *, dir_fd, follow_symlinks):
            assert follow_symlinks is False
            node = self.entries[self.key(name, dir_fd)]
            self.calls.append(('lchown', node.st_ino))
            node.st_uid, node.st_gid = uid, gid

        def removexattr(self, fd, attr):
            self.calls.append(('acl', self.fds[fd][0].st_ino))

        @contextmanager
        def directory(self, path):
            fd = self.descriptor(())
            try:
                yield fd
            finally:
                self.fds.pop(fd)

    fs = Tree()
    fakeos = SimpleNamespace(**{name: getattr(os, name) for name in
        ('O_RDONLY', 'O_RDWR', 'O_CREAT', 'O_EXCL', 'SEEK_SET')})
    fakeos.O_NOFOLLOW, fakeos.O_NONBLOCK, fakeos.O_DIRECTORY = 0x100000, 0x200000, 0x400000
    for name in ('stat', 'fstat', 'listdir', 'open', 'read', 'write', 'replace', 'fchown',
                 'fchmod', 'chown', 'removexattr'):
        setattr(fakeos, name, getattr(fs, name))
    fakeos.close = lambda fd: fs.fds.pop(fd)
    fakeos.fsync = lambda fd: fs.calls.append(('fsync', fs.fds[fd][0].st_ino))
    fakeos.lseek = lambda fd, pos, whence: fs.fds[fd].__setitem__(2, pos)
    fakeos.ftruncate = lambda fd, size: fs.fds[fd][0].data.__delitem__(slice(size, None))
    monkeypatch.setattr(module, 'os', fakeos)
    monkeypatch.setattr(module, 'directory', fs.directory)
    monkeypatch.setattr(module, 'mount_points', lambda: set())
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: fs.calls.append(('setfacl', a[0])))
    fs.module, fs.ids = module, runtime.fixture.ids
    fs.manifest, fs.saved = {}, []
    fs.save = lambda: fs.saved.append(copy.deepcopy(fs.manifest))
    fs.transfer = lambda: module.transfer(Path('/lane'), fs.ids, fs.manifest, fs.save)
    return fs


@pytest.mark.parametrize('target', [b'../own-file', b'/other-lane/service-secret', b'missing'])
def test_worker_symlinks_lchowned_without_following(tree, target):
    tree.add('projects', stat.S_IFDIR | 0o700)
    link = tree.add('projects/link', stat.S_IFLNK | 0o777, target)
    tree.transfer()
    assert bytes(link.data) == target and link.st_uid == tree.ids['worker_uid']
    assert ('projects', 'link') not in tree.opens
    assert ('lchown', link.st_ino) in tree.calls
    assert not any(call == ('chmod', link.st_ino) for call in tree.calls)
    assert tree.manifest['entries']['projects/link']['ino'] == link.st_ino


@pytest.mark.parametrize('mode', [stat.S_IFIFO, stat.S_IFSOCK])
def test_worker_fifo_and_socket_are_not_opened(tree, mode):
    tree.add('cli', stat.S_IFDIR | 0o700)
    node = tree.add('cli/endpoint', mode | 0o600)
    tree.transfer()
    assert node.st_uid == tree.ids['worker_uid']
    assert ('cli', 'endpoint') not in tree.opens
    assert ('lchown', node.st_ino) in tree.calls


@pytest.mark.parametrize('mode', [stat.S_IFCHR, stat.S_IFBLK])
def test_worker_devices_refused_before_mutation(tree, mode):
    tree.add('scratch', stat.S_IFDIR | 0o700)
    tree.add('scratch/device', mode | 0o600)
    with pytest.raises(ValueError, match='scratch/device'):
        tree.transfer()
    assert not tree.calls and not tree.manifest


@pytest.mark.parametrize('mode,nlink', [(stat.S_IFLNK, 1), (stat.S_IFIFO, 1),
                                     (stat.S_IFSOCK, 1), (stat.S_IFREG, 2)])
def test_service_refusals_identify_relative_path_without_data(tree, mode, nlink):
    tree.add('service', stat.S_IFDIR | 0o700)
    node = tree.add('service/offender', mode | 0o600, b'SECRET_DO_NOT_LOG')
    node.st_nlink = nlink
    with pytest.raises(ValueError, match='service/offender') as exc:
        tree.transfer()
    assert 'SECRET_DO_NOT_LOG' not in str(exc.value) and not tree.calls


def hardlinked_project(tree):
    tree.add('scratch', stat.S_IFDIR | 0o700)
    original = tree.add('scratch/shared', stat.S_IFREG | 0o6750, b'preserve-content' * 5)
    # A name outside this lane, e.g. another lane's service state, pins it.
    original.st_nlink = 2
    return original


def test_hardlink_copy_leaves_foreign_inode_owner_mode_acl_and_data_untouched(tree):
    original = hardlinked_project(tree)
    before = copy.deepcopy(original)
    tree.transfer()
    copied = tree.entries[('scratch', 'shared')]
    assert copied.st_ino != original.st_ino and copied.st_nlink == 1
    assert copied.st_uid == tree.ids['worker_uid'] and stat.S_IMODE(copied.st_mode) == 0o770
    assert copied.data == before.data == original.data
    assert (original.st_uid, original.st_gid, original.st_mode) == (before.st_uid, before.st_gid, before.st_mode)
    assert not any((kind, original.st_ino) in tree.calls for kind in ('chown', 'chmod', 'acl'))
    assert tree.manifest['link_breaks']['scratch/shared']['completed']
    count = len(tree.calls)
    tree.transfer()
    assert len(tree.calls) == count


@pytest.mark.parametrize('crash', ['created', 'copy', 'prepared', 'renamed'])
def test_hardlink_crash_then_durable_replay_never_loses_original_name(tree, monkeypatch, crash):
    original = hardlinked_project(tree)
    write, replace = tree.module.os.write, tree.module.os.replace
    save = tree.save
    armed = True
    def interrupt_save():
        nonlocal armed
        save()
        records = tree.manifest.get('link_breaks', {})
        record = records.get('scratch/shared', {})
        if armed and crash == 'prepared' and record.get('ready') and not record.get('completed'):
            armed = False
            raise RuntimeError('power loss')
    def interrupt_write(fd, data):
        nonlocal armed
        result = write(fd, data)
        if armed and crash == 'copy':
            armed = False
            raise RuntimeError('power loss')
        return result
    def interrupt_replace(*args, **kwargs):
        nonlocal armed
        replace(*args, **kwargs)
        if armed and crash == 'renamed':
            armed = False
            raise RuntimeError('power loss')
    openfd = tree.module.os.open
    def interrupt_open(name, flags, *args, **kwargs):
        nonlocal armed
        fd = openfd(name, flags, *args, **kwargs)
        if armed and crash == 'created' and flags & os.O_CREAT:
            armed = False
            raise RuntimeError('power loss')
        return fd
    monkeypatch.setattr(tree.module.os, 'open', interrupt_open)
    monkeypatch.setattr(tree.module.os, 'write', interrupt_write)
    monkeypatch.setattr(tree.module.os, 'replace', interrupt_replace)
    tree.save = interrupt_save
    with pytest.raises(RuntimeError, match='power loss'):
        tree.transfer()
    current = tree.entries[('scratch', 'shared')]
    assert current.data == original.data  # Always original or completed copy.
    tree.manifest = copy.deepcopy(tree.saved[-1])  # Discard non-durable state.
    tree.save = save
    tree.transfer()
    copied = tree.entries[('scratch', 'shared')]
    assert copied.data == original.data and copied.st_ino != original.st_ino
    assert tree.manifest['link_breaks']['scratch/shared']['completed']
    assert not any(p and p[-1].startswith('.bob-migrate-') for p in tree.entries)
    assert not any((kind, original.st_ino) in tree.calls for kind in ('chown', 'chmod', 'acl'))


@pytest.mark.parametrize('change', ['missing', 'replaced-link', 'replaced-copy'])
def test_inventory_detects_missing_links_and_replaced_copies(tree, monkeypatch, change):
    tree.add('scratch', stat.S_IFDIR | 0o700)
    tree.add('scratch/link', stat.S_IFLNK | 0o777, b'/other-lane/file')
    hardlinked_project(tree)
    def stop():
        tree.saved.append(copy.deepcopy(tree.manifest))
        if tree.manifest.get('inventory_complete') and not tree.manifest.get('applied'):
            raise RuntimeError('inventory checkpoint')
    tree.save = stop
    with pytest.raises(RuntimeError):
        tree.transfer()
    tree.save = lambda: tree.saved.append(copy.deepcopy(tree.manifest))
    if change == 'missing':
        del tree.entries[('scratch', 'link')]
    elif change == 'replaced-link':
        tree.add('scratch/link', stat.S_IFLNK | 0o777, b'/changed')
    else:
        # Obtain a completed break but interrupt before the applied checkpoint.
        native = tree.module.os.replace
        def after(*a, **k):
            native(*a, **k)
            raise RuntimeError('after rename')
        monkeypatch.setattr(tree.module.os, 'replace', after)
        with pytest.raises(RuntimeError):
            tree.transfer()
        monkeypatch.setattr(tree.module.os, 'replace', native)
        tree.add('scratch/shared', stat.S_IFREG | 0o600, b'changed')
    with pytest.raises(ValueError, match='inventory'):
        tree.transfer()


@pytest.mark.parametrize('error', [None, errno.ENODATA, errno.ENOTSUP, errno.EPERM])
def test_acl_cleanup_uses_fd_xattrs_and_only_default_acl_directories_spawn(tree, monkeypatch, error):
    fd = tree.descriptor(())
    attrs = []
    def remove(fdarg, attr):
        assert fdarg == fd
        attrs.append(attr)
        if error:
            raise OSError(error, 'xattr error')
    monkeypatch.setattr(tree.module.os, 'removexattr', remove)
    if error == errno.EPERM:
        with pytest.raises(OSError):
            tree.module.apply(fd, 1, 1, 0o700)
        assert not tree.calls
    else:
        tree.module.apply(fd, 1, 1, 0o700)
        assert attrs == ['system.posix_acl_access', 'system.posix_acl_default']
        assert not any(call[0] == 'setfacl' for call in tree.calls)


def test_rotation_secrets_scrubbed_durably_after_publish_and_recovery(runtime, monkeypatch):
    m = runtime
    fake_phases(m, monkeypatch)
    native = m.credentials
    def credentials(name, root, state, persist):
        native(name, root, state, persist)
        state['rotation'].update(jwt='NEW_JWT_SYNTHETIC', vouch='NEW_VOUCH_SYNTHETIC', keys_published=True)
        persist()
    monkeypatch.setattr(m, 'credentials', credentials)
    m.migrate('alpha')
    journal = (m.STATE / 'alpha/state.json').read_text()
    assert 'NEW_JWT_SYNTHETIC' not in journal and 'NEW_VOUCH_SYNTHETIC' not in journal
    assert m.state_for('alpha')['rotation'] == dict(keys_published=True, portal_reissued=True)
    m.suspend('alpha')
    m.migrate('alpha')
    assert m.state_for('alpha')['rotation'] == dict(keys_published=True, portal_reissued=True)


def test_two_worker_names_for_same_inode_both_get_private_copies(tree):
    original = hardlinked_project(tree)
    # Replace the simulated foreign name with a second in-tree name.
    original.st_nlink = 1
    tree.link('scratch/shared', 'scratch/second')
    tree.transfer()
    one, two = tree.entries[('scratch', 'shared')], tree.entries[('scratch', 'second')]
    assert one.st_ino != two.st_ino != original.st_ino
    assert one.st_nlink == two.st_nlink == 1 and one.data == two.data == original.data
    assert len(tree.manifest['link_breaks']) == 2
    assert not any((kind, original.st_ino) in tree.calls for kind in ('chown', 'chmod', 'acl'))


def test_hardlink_checkpoints_do_not_reserialize_full_inventory(tree):
    hardlinked_project(tree)
    receipts = []
    tree.module.transfer(Path('/lane'), tree.ids, tree.manifest, tree.save,
                         checkpoint=lambda key, record: receipts.append((key, copy.deepcopy(record))))
    assert len(tree.saved) == 2  # Inventory and applied-tree checkpoint only.
    assert all(key == 'scratch/shared' for key, _ in receipts)
    assert receipts[0][1]['temporary'] and receipts[-1][1]['completed']
    assert any(record.get('ready') and not record.get('completed') for _, record in receipts)


def test_ownership_replay_merges_durable_small_break_receipts(runtime, monkeypatch):
    m = runtime
    key = 'scratch/shared'
    state = {'ownership': {'inventory_complete': True, 'entries': {key: {'ino': 42}}}, 'helpers': {}}
    record = {'temporary': '.bob-migrate-synthetic', 'ready': True, 'copy': {'dev': 1, 'ino': 43}}
    attempts = []
    def transfer(root, ids, manifest, persist, *, checkpoint):
        attempts.append(copy.deepcopy(manifest))
        checkpoint(key, record)
        raise RuntimeError('power loss before full state checkpoint')
    monkeypatch.setattr(m, 'transfer', transfer)
    before = copy.deepcopy(state)
    with pytest.raises(RuntimeError):
        m.ownership('alpha', m.fixture.root, state, lambda: None)
    with pytest.raises(RuntimeError):
        m.ownership('alpha', m.fixture.root, before, lambda: None)
    assert not attempts[0].get('link_breaks')
    assert attempts[1]['link_breaks'][key] == record


@pytest.mark.parametrize('kind', ['device', 'bind'])
def test_worker_mount_crossings_still_refused(tree, monkeypatch, kind):
    tree.add('projects', stat.S_IFDIR | 0o700)
    tree.add('projects/mounted', stat.S_IFDIR | 0o700, dev=2 if kind == 'device' else 1)
    if kind == 'bind':
        monkeypatch.setattr(tree.module, 'mount_points', lambda: {str(Path('/lane').absolute()) + '/projects/mounted'})
    with pytest.raises(ValueError, match='projects/mounted'):
        tree.transfer()
    assert not tree.calls


def test_default_acls_spawn_only_for_project_and_scratch_directories(tree):
    for name in ('projects', 'scratch', 'cli', 'service', 'codex-home'):
        tree.add(name, stat.S_IFDIR | 0o700)
        tree.add(name + '/file', stat.S_IFREG | 0o600)
    tree.transfer()
    commands = [call[1] for call in tree.calls if call[0] == 'setfacl']
    assert len(commands) == 2
    assert all(command[:2] == ['setfacl', '-m'] for command in commands)


@pytest.mark.skipif(os.name == 'nt', reason='real Linux dirfd/link/special-file syscalls')
def test_real_worker_tree_links_and_endpoints(runtime, monkeypatch, tmp_path):
    import socket
    module = runtime.fixture.modules['migration_paths']
    root = tmp_path / 'lane'
    (root / 'scratch').mkdir(parents=True)
    outside = tmp_path / 'foreign-service'
    outside.write_bytes(b'foreign inode remains unchanged')
    outside.chmod(0o640)
    original = outside.stat()
    (root / 'scratch/symlink').symlink_to(outside)
    os.link(outside, root / 'scratch/hardlink')
    os.mkfifo(root / 'scratch/fifo', 0o600)
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(root / 'scratch/socket'))
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: None)
    ids = {role + suffix: value for role in ('worker', 'service')
           for suffix, value in (('_uid', os.getuid()), ('_gid', os.getgid()))}
    manifest = {}
    try:
        module.transfer(root, ids, manifest, lambda: None)
        assert (root / 'scratch/symlink').is_symlink()
        assert os.readlink(root / 'scratch/symlink') == str(outside)
        assert (root / 'scratch/hardlink').stat().st_ino != original.st_ino
        assert (root / 'scratch/hardlink').read_bytes() == outside.read_bytes()
        current = outside.stat()
        assert (current.st_ino, current.st_uid, current.st_gid, current.st_mode) == (
            original.st_ino, original.st_uid, original.st_gid, original.st_mode)
        assert stat.S_ISFIFO((root / 'scratch/fifo').lstat().st_mode)
        assert stat.S_ISSOCK((root / 'scratch/socket').lstat().st_mode)
    finally:
        sock.close()


def test_migrating_a_lane_restarts_already_migrated_lanes(runtime, monkeypatch):
    # Lane cores Require bob-qdrant; migrating another lane stops it, which also
    # stops completed lanes. They must be started again afterwards.
    m = runtime
    monkeypatch.setattr(m.lane, 'lanes', lambda: [('alpha', None), ('beta', None), ('gamma', None)])
    for name, state in (('beta', dict(phase='completed', activation_complete=True)),
                        ('gamma', dict(phase='verify'))):
        (m.STATE / name).mkdir(parents=True)
        (m.STATE / name / 'state.json').write_text(json.dumps(dict(state, lane=name, version=m.VERSION)))
    monkeypatch.setattr(m, 'read_env', lambda path: {'lane': path.parent.parent.name})
    m.fixture.calls.clear()
    m.restore_migrated('alpha')
    started = [call for call in m.fixture.calls if call and call[0] == 'start']
    assert started == [('start', 'bob-qdrant.service', 'bob-embedder.service', *m.lane.units('beta'))]
    # It must wait for the restarted lane before returning.
    assert m.fixture.calls[-1] == ('wait-ready',)
