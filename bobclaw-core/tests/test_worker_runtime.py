"""Fresh-lane identity journal and operator writers, with OS administration mocked."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parents[2] / 'deploy/runtime/lib'


@pytest.fixture
def worker(tmp_path, monkeypatch):
    accounts = {}
    def getpwnam(name):
        if name not in accounts:
            raise KeyError(name)
        return accounts[name]
    def getpwuid(uid):
        for account in accounts.values():
            if account.pw_uid == uid:
                return account
        raise KeyError(uid)
    monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace(getpwnam=getpwnam, getpwuid=getpwuid))
    for name in ('common', 'worker'):
        spec = importlib.util.spec_from_file_location(name, RUNTIME / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
    module = sys.modules['worker']
    monkeypatch.setattr(module, 'IDENTITIES', tmp_path / 'identities')
    monkeypatch.setattr(module, 'require_root', lambda: None)
    monkeypatch.setattr(module, 'private_replace', lambda path, content, *args: path.write_text(content))
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'useradd':
            uid = int(argv[argv.index('--uid') + 1])
            accounts[argv[-1]] = SimpleNamespace(pw_name=argv[-1], pw_uid=uid, pw_gid=uid,
                pw_gecos=argv[argv.index('--comment') + 1])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    return module, accounts, calls


def test_persistent_identities_are_idempotent_and_lane_scoped(worker):
    module, accounts, calls = worker
    a = module.identity('a' * 32)
    assert a['phase'] == 'ready' and a['worker_uid'] != a['service_uid']
    assert len(a['worker_user']) < 32
    assert module.identity('a' * 32) == a
    assert len([c for c in calls if c[0] == 'useradd']) == 4
    assert len({a[r+'_uid'] for r in ('service','worker','gateway','bridge')}) == 4
    b = module.identity('b')
    assert len({a['worker_uid'], a['service_uid'], b['worker_uid'], b['service_uid']}) == 4
    assert (module.IDENTITIES / ('a' * 32 + '.json')).exists()


def test_identity_journal_resumes_interruption(worker, monkeypatch):
    module, accounts, calls = worker
    run = module.subprocess.run
    def interrupted(argv, **kwargs):
        result = run(argv, **kwargs)
        if argv[0] == 'useradd':
            raise RuntimeError('power loss after account creation')
        return result
    monkeypatch.setattr(module.subprocess, 'run', interrupted)
    with pytest.raises(RuntimeError):
        module.identity('a')
    assert len(accounts) == 1
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.identity('a')['phase'] == 'ready' and len(accounts) == 4


def test_removed_account_uid_is_still_reserved_by_the_mapping(worker):
    module, accounts, _ = worker
    old = module.identity('retained')
    del accounts[old['service_user']]
    del accounts[old['worker_user']]
    new = module.identity('new-lane')
    assert {new['service_uid'], new['worker_uid']}.isdisjoint({old['service_uid'], old['worker_uid']})


def test_existing_isolated_identity_upgrades_without_reassigning_old_uids(worker):
    module, accounts, calls = worker
    full = module.identity('old-isolated')
    old = {k:v for k,v in full.items() if not k.startswith(('gateway_', 'bridge_'))}
    for role in ('gateway','bridge'):
        del accounts[full[role+'_user']]
    path = module.IDENTITIES / 'old-isolated.json'
    path.write_text(json.dumps(old))
    calls.clear()
    upgraded = module.identity('old-isolated')
    assert all(upgraded[k] == old[k] for k in old)
    assert len({upgraded[r+'_uid'] for r in ('service','worker','gateway','bridge')}) == 4
    assert len([c for c in calls if c[0] == 'useradd']) == 2
    assert module.identity('old-isolated') == upgraded


def test_interrupted_gateway_uid_upgrade_resumes_allocation(worker, monkeypatch):
    module, accounts, calls = worker
    full = module.identity('old-isolated')
    old = {k:v for k,v in full.items() if not k.startswith(('gateway_', 'bridge_'))}
    for role in ('gateway','bridge'):
        del accounts[full[role+'_user']]
    path = module.IDENTITIES / 'old-isolated.json'
    path.write_text(json.dumps(old))
    native = module.subprocess.run
    def interrupted(argv, **kwargs):
        result = native(argv, **kwargs)
        if argv[0] == 'useradd' and argv[-1].startswith('bob-g-'):
            raise RuntimeError('power loss after gateway account creation')
        return result
    monkeypatch.setattr(module.subprocess, 'run', interrupted)
    with pytest.raises(RuntimeError):
        module.identity('old-isolated')
    assert json.loads(path.read_text())['phase'] == 'allocating'
    monkeypatch.setattr(module.subprocess, 'run', native)
    upgraded = module.identity('old-isolated')
    assert upgraded['phase'] == 'ready'
    assert upgraded['service_uid'] == old['service_uid'] and upgraded['worker_uid'] == old['worker_uid']
    assert len({upgraded[r+'_uid'] for r in ('service','worker','gateway','bridge')}) == 4


def test_uid_drift_and_hash_collision_fail_closed(worker):
    module, accounts, calls = worker
    a = module.identity('a')
    accounts[a['worker_user']].pw_uid += 500
    with pytest.raises(ValueError, match='identity changed'):
        module.identity('a')
    # A durable other-name record occupies B's proposed short ID.
    lane_id = module.hashlib.sha256(b'b').hexdigest()[:12]
    (module.IDENTITIES / 'collision.json').write_text(json.dumps({'lane_id': lane_id}))
    with pytest.raises(ValueError, match='collision'):
        module.identity('b')


def test_nft_policy_covers_uid_local_ipv6_and_fixed_resolver(worker):
    module, _, _ = worker
    rules = module.nft_rules({'worker_uid': 123, 'lane_id': 'abc'}, '10.255.255.254')
    assert 'meta skuid 123 jump worker' in rules
    assert 'fib daddr type local reject' in rules
    from ipaddress import ip_network
    shared_address_space = str(ip_network('100.65.0.0/10', strict=False))
    assert '127.0.0.0/8' in rules and shared_address_space in rules
    assert 'ip6 daddr != 2000::/3 reject' in rules
    assert 'ip daddr 10.255.255.254 udp dport 53 accept' in rules
    assert 'tcp dport 443 accept' in rules
    assert rules.rfind('reject') > rules.rfind('tcp dport 443 accept')
    with pytest.raises(ValueError):
        module.nft_rules({'worker_uid': 123, 'lane_id': 'abc'}, '$(anything)')


def test_service_environment_split(worker, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from core.worker import auth_file
    module, _, _ = worker
    common = sys.modules['common']
    ids = module.identity('a')
    root = tmp_path / 'a/bob-memory'
    root.mkdir(parents=True)
    shared = tmp_path / 'data/secrets'
    shared.mkdir(parents=True)
    (shared / 'bobclaw.env').write_text('POSTGRES_URL="db-canary"\nREDIS_URL="redis"\nPOSTGRES_PASSWORD="never-export"\n')
    (root / 'lane.env').write_text('BOB_BRIDGE_ENGINE="http://127.0.0.1:1"\nBOB_BRIDGE_PORT="2"\n'
        'BOBCLAW_SECRET="vouch"\nJWT_SECRET="jwt"\nTOTP_SECRET="totp"\nBOBCLAW_PASSWORD="password"\n')
    monkeypatch.setattr(common, 'DATA', tmp_path / 'data')
    monkeypatch.setattr(module, 'SERVICE_ENVS', tmp_path / 'service-env')
    @contextmanager
    def directory(path, **kwargs):
        path.mkdir(parents=True, exist_ok=True)
        yield 42
    monkeypatch.setattr(auth_file, 'directory', directory)
    monkeypatch.setattr(module.os, 'fchown', lambda *a: None, raising=False)
    monkeypatch.setattr(module.os, 'fchmod', lambda *a: None, raising=False)
    monkeypatch.setattr(module.os, 'chown', lambda *a: None, raising=False)
    monkeypatch.setattr(module.os, 'chmod', lambda *a: None)
    module.service_environments(root)
    core = common.read_env(root / 'core.env')
    gateway = common.read_env(module.SERVICE_ENVS / (ids['lane_id']+'-gateway.env'))
    bridge = common.read_env(module.SERVICE_ENVS / (ids['lane_id']+'-bridge.env'))
    assert core['BOBCLAW_SECRET'] == 'vouch' and 'JWT_SECRET' not in core and 'TOTP_SECRET' not in core
    assert gateway['JWT_SECRET'] == 'jwt' and gateway['BOBCLAW_PASSWORD'] == 'password'
    assert set(bridge) == {'BOB_BRIDGE_ENGINE', 'BOB_BRIDGE_PORT', 'BOB_SETTINGS_ID', 'BOB_LANE_ROOT'}
    assert not (root / 'gateway.env').exists()
    assert gateway['GATEWAY_SQLITE_PATH'].startswith('/var/lib/bob-gateway/') or 'bob-gateway' in gateway['GATEWAY_SQLITE_PATH']
    assert 'BOBCLAW_PASSWORD' not in common.read_env(root / 'public.env')
    assert all('POSTGRES_PASSWORD' not in env for env in (core, gateway, bridge))
    # Actual reader uses public metadata instead of opening the private operator
    # env or CLI/provider credential files under the new gateway/bridge UID.
    from core.lane_config import read_lane
    monkeypatch.setenv('BOB_SETTINGS_ID', ids['lane_id'])
    monkeypatch.setenv('BOB_SERVICE_ROLE', 'gateway')
    original = (root / 'lane.env').read_text()
    (root / 'lane.env').write_text('INVALID PRIVATE ENV MUST NOT BE READ')
    assert read_lane(root).values['BOB_SETTINGS_ID'] == ids['lane_id']
    (root / 'lane.env').write_text(original)


def test_fresh_lane_retry_preserves_secrets_ports_and_admission(worker, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from core.worker import auth_file
    module, _, _ = worker
    monkeypatch.setitem(sys.modules, 'fcntl', SimpleNamespace())
    spec = importlib.util.spec_from_file_location('lane', RUNTIME / 'lane.py')
    lane = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'lane', lane)
    spec.loader.exec_module(lane)
    data = tmp_path / 'data'
    data.mkdir()
    (data / 'family').mkdir()
    (data / '.firstboot-complete').touch()
    monkeypatch.setattr(lane, 'DATA', data)
    monkeypatch.setattr(lane, 'FAMILY', data / 'family')
    ids = {'lane_id': 'abc', 'worker_uid': 101, 'worker_gid': 101, 'service_uid': 102, 'service_gid': 102}
    monkeypatch.setattr(module, 'identity', lambda name: ids)
    monkeypatch.setattr(module, 'load_identity', lambda name: ids)
    @contextmanager
    def directory(path, **kwargs):
        path.mkdir(parents=True, exist_ok=True)
        yield 42
    monkeypatch.setattr(auth_file, 'directory', directory)
    monkeypatch.setattr(lane.os, 'fchown', lambda *args: None, raising=False)
    monkeypatch.setattr(lane.os, 'fchmod', lambda *args: None, raising=False)
    monkeypatch.setattr(lane, 'private_write', lambda path, content, *args, **kwargs: path.write_text(content))
    monkeypatch.setattr(lane, 'port_available', lambda port: True)
    def failure(*args):
        raise ValueError('network policy unavailable')
    monkeypatch.setattr(module, 'provision', failure)
    starts = []
    monkeypatch.setattr(lane, 'systemctl', lambda *args: starts.append(args))
    monkeypatch.setattr(lane, 'complete_fresh', lambda *args: None)
    with pytest.raises(ValueError, match='network policy unavailable'):
        lane.add('alpha', 'original-password')
    assert not starts
    root = data / 'family/alpha/bob-memory'
    original = (root / 'lane.env').read_bytes()
    stored = json.loads((data / 'lane-provisioning/alpha.json').read_text())['values']
    assert stored['BOBCLAW_PASSWORD'] == 'original-password'
    monkeypatch.setattr(module, 'provision', lambda *args: None)
    lane.add('alpha', 'a-different-retry-password')
    assert (root / 'lane.env').read_bytes() == original
    assert starts and lane.reserved_ports() >= {int(stored[k]) for k in
        ('BOBCLAW_CORE_PORT', 'BOBCLAW_GATEWAY_PORT', 'BOB_BRIDGE_PORT')}


def test_embedded_runtime_probe_programs_parse():
    import ast
    script = RUNTIME.parent / 'tests/acceptance.py'
    tree = ast.parse(script.read_text(encoding='utf-8'))
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not any(isinstance(t, ast.Name) and t.id in {'code', 'source'} for t in node.targets):
            continue
        value = node.value
        while isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute) and value.func.attr == 'replace':
            value = value.func.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            source = value.value
        elif isinstance(value, ast.JoinedStr):
            source = ''.join(part.value if isinstance(part, ast.Constant) else repr('/tmp/probe') for part in value.values)
        else:
            continue
        for name, replacement in {'PATHS': '[]', 'PIDS': '[]', 'ENDPOINTS': '[]',
                                  'SOCKETS': '[]', 'COMMANDS': '[]', 'HOOK': repr('import os'), 'LANE': repr('probe')}.items():
            source = source.replace(name, replacement)
        ast.parse(source)
        checked += 1
    assert checked >= 10
