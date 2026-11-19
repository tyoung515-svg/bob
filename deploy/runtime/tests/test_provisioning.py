"""Provision real files from an empty /etc/bob; stub host accounts/activation only.

Run as root on Linux. These tests use real dirfds, ownership, modes, SQLite and
atomic publication. They do not assert kernel ACL or systemd enforcement.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.skipif(os.name == 'nt' or getattr(os, 'geteuid', lambda: -1)() != 0,
                               reason='requires real root Linux provisioning syscalls')
ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'bobclaw-core'))
    monkeypatch.syspath_prepend(str(ROOT / 'deploy/runtime/lib'))
    modules = {}
    for name in ('common', 'worker', 'lane', 'settings', 'credentials'):
        spec = importlib.util.spec_from_file_location(name, ROOT / 'deploy/runtime/lib' / (name + '.py'))
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[name] = module
    common, worker, lane, settings = [modules[n] for n in ('common', 'worker', 'lane', 'settings')]
    data = tmp_path / 'var/lib/bob'
    (data / 'secrets').mkdir(parents=True)
    (data / '.firstboot-complete').touch()
    (data / 'secrets/bobclaw.env').write_text(common.render_env({
        'POSTGRES_URL': 'postgresql://synthetic', 'REDIS_URL': 'redis://127.0.0.1:6379/0'}))
    systemd = tmp_path / 'etc/systemd/system'
    systemd.mkdir(parents=True)
    for module in (common, worker, settings):
        monkeypatch.setattr(module, 'SYSTEMD', systemd)
    monkeypatch.setattr(common, 'DATA', data)
    monkeypatch.setattr(common, 'MIGRATIONS', tmp_path / 'var/lib/bob-worker-migrations')
    monkeypatch.setattr(common, 'execution_mode', lambda: 'worker')
    monkeypatch.setattr(lane, 'DATA', data)
    monkeypatch.setattr(lane, 'FAMILY', data / 'family')
    monkeypatch.setattr(lane, 'renew_if_due', lambda name: False)
    monkeypatch.setattr(lane, 'port_available', lambda port: True)
    for name, path in (('CONFIG', 'etc/bob/workers'), ('IDENTITIES', 'etc/bob/lane-identities'),
                       ('SERVICE_ENVS', 'etc/bob/service-env'), ('GATEWAY_STATE', 'var/lib/bob-gateway')):
        monkeypatch.setattr(worker, name, tmp_path / path)
    # Re-root the two literal worker/journal state paths used by provision().
    monkeypatch.setattr(worker, 'Path', lambda value: tmp_path / Path(value).relative_to('/'))
    monkeypatch.setattr(settings, 'CONFIG', tmp_path / 'etc/bob/settings')
    monkeypatch.setattr(settings, 'STATE', tmp_path / 'var/lib/bob-settings')
    monkeypatch.setattr(settings, 'RUN', tmp_path / 'run/bob-settings')
    monkeypatch.setattr(settings, 'TMPFILES', tmp_path / 'etc/tmpfiles.d')
    from core import settings_client
    monkeypatch.setattr(settings_client, 'VAULT_STATE', settings.STATE)

    accounts = {'bobapp': SimpleNamespace(pw_uid=60000, pw_gid=60000)}
    def getpwnam(name):
        if name not in accounts:
            raise KeyError(name)
        return accounts[name]
    def getpwuid(uid):
        for account in accounts.values():
            if account.pw_uid == uid:
                return account
        raise KeyError(uid)
    monkeypatch.setattr(worker.pwd, 'getpwnam', getpwnam)
    monkeypatch.setattr(worker.pwd, 'getpwuid', getpwuid)
    def host_command(args, **kwargs):
        assert args[0] in {'useradd', 'usermod', 'setfacl'}
        if args[0] == 'useradd':
            uid = int(args[args.index('--uid') + 1])
            accounts[args[-1]] = SimpleNamespace(pw_uid=uid, pw_gid=uid,
                                               pw_gecos=args[args.index('--comment') + 1])
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(worker.subprocess, 'run', host_command)
    calls = []
    for module in (common, worker, lane):
        monkeypatch.setattr(module, 'systemctl', lambda *args: calls.append(args))
    previous = os.umask(0o077)
    try:
        yield SimpleNamespace(**modules, base=tmp_path, data=data, systemd=systemd, calls=calls)
    finally:
        os.umask(previous)


def assert_permissions(path, mode, uid=0, gid=0):
    info = path.stat()
    assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (uid, gid, mode), path


def assert_layout(r, root, ids):
    home = r.settings.STATE / ids['lane_id']
    for path, mode, gid in (
            (r.base / 'etc/bob', 0o755, 0), (r.worker.IDENTITIES, 0o700, 0),
            (r.worker.CONFIG, 0o755, 0), (r.worker.SERVICE_ENVS, 0o700, 0),
            (r.settings.CONFIG, 0o755, 0), (r.settings.STATE, 0o711, 0),
            (home, 0o711, 0), (home / 'vault', 0o750, ids['service_gid']),
            (home / 'credentials', 0o711, 0)):
        assert_permissions(path, mode, gid=gid)
    for path in r.systemd.glob('*.d'):
        assert_permissions(path, 0o755)
        for dropin in path.iterdir():
            assert_permissions(dropin, 0o600)
    for path in r.worker.SERVICE_ENVS.iterdir():
        assert_permissions(path, 0o600)
    for name, mode in (('active.json', 0o644), ('current.env', 0o600), ('initial.json', 0o600)):
        assert_permissions(home / 'credentials' / name, mode)
    assert_permissions(root / 'cli', 0o711)
    assert_permissions(root / 'cli/claude', 0o700, ids['worker_uid'], ids['worker_gid'])
    config = json.loads((r.worker.CONFIG / (ids['lane_id'] + '.json')).read_text())
    assert config['claude_home'] == str(root / 'cli/claude')
    assert config['profiles']['claude'] == dict(argv=['/usr/local/bin/claude'],
                                               network=True, egress='lane-proxy/1')
    assert_permissions(root / 'settings.json', 0o644)
    assert_permissions(root / 'lane.env', 0o600)
    assert_permissions(r.worker.CONFIG / (ids['lane_id'] + '.json'), 0o644)
    assert_permissions(r.settings.CONFIG / (ids['lane_id'] + '.json'), 0o600)
    assert_permissions(r.worker.GATEWAY_STATE, 0o711)
    assert_permissions(r.worker.GATEWAY_STATE / ids['lane_id'], 0o2750,
                       ids['gateway_uid'], ids['service_gid'])


def test_fresh_lane_add_from_empty_etc_bob_under_umask_077(runtime):
    r = runtime
    assert not (r.base / 'etc/bob').exists()
    r.lane.add('fresh', 'fresh-password-canary')
    root = r.lane.FAMILY / 'fresh/bob-memory'
    ids = r.worker.load_identity('fresh')
    home = r.settings.STATE / ids['lane_id'] / 'credentials'
    generation = json.loads((home / 'initial.json').read_text())
    lane = r.common.read_env(root / 'lane.env', safe=True)
    assert lane['BOB_OWNER_WORK_APPROVAL'] == 'signed_in_user'
    gateway = r.common.read_env(r.worker.SERVICE_ENVS / (ids['lane_id'] + '-gateway.env'), safe=True)
    assert gateway['BOB_OWNER_WORK_APPROVAL'] == 'signed_in_user'
    assert gateway['RATE_LIMIT_PER_MINUTE'] == '600'
    assert gateway['RATE_LIMIT_BURST'] == '300'
    assert 'BOB_OWNER_WORK_APPROVAL' not in r.common.read_env(root / 'core.env', safe=True)
    assert 'BOB_OWNER_WORK_APPROVAL' not in r.common.read_env(root / 'public.env', safe=True)
    assert generation['password'] == lane['BOBCLAW_PASSWORD'] == 'fresh-password-canary'
    assert generation['totp_secret'] == lane['TOTP_SECRET'] and generation['epoch'] == 1
    assert r.common.read_env(home / 'current.env', safe=True)['BOB_CREDENTIAL_EPOCH'] == '1'
    assert not (r.worker.GATEWAY_STATE / ids['lane_id'] / 'gateway.db').exists()
    assert any(call[:2] == ('enable', '--now') for call in r.calls)
    assert_layout(r, root, ids)


@pytest.mark.parametrize('provider', ['anthropic', 'deepseek', 'google', 'glm', 'kimi', 'qwen'])
def test_settings_enrollment_regenerates_real_private_and_public_environments(runtime, monkeypatch, provider):
    r = runtime
    r.lane.add('fresh', 'fresh-password-canary')
    root = r.lane.FAMILY / 'fresh/bob-memory'
    ids = r.worker.load_identity('fresh')
    spec = importlib.util.spec_from_file_location('model', ROOT / 'deploy/runtime/lib/model.py')
    model = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, 'model', model)
    spec.loader.exec_module(model)
    monkeypatch.setattr(model, 'FAMILY', r.lane.FAMILY)
    monkeypatch.setattr(model, 'execution_mode', lambda: 'worker')
    monkeypatch.setattr(model, 'systemctl', lambda *a: r.calls.append(a))
    native_open = open
    monkeypatch.setattr(model, 'open', lambda path, mode: native_open(
        r.base / 'run' / Path(path).name if str(path).startswith('/run/') else path, mode), raising=False)
    monkeypatch.delenv('BOB_SETTINGS_ID', raising=False)
    monkeypatch.delenv('BOB_SERVICE_ROLE', raising=False)
    cfg = r.settings.config(ids['lane_id'])
    gateway_env = r.worker.SERVICE_ENVS / (ids['lane_id'] + '-gateway.env')
    gateway_before = gateway_env.read_bytes()
    r.calls.clear()
    receipt = r.settings.handle(cfg, ids['gateway_uid'], dict(version=1, op='secrets.set',
        provider=provider, key='fake-provider-canary', request_key='enrollment'))
    setup = model.SETUP[provider]
    public = r.common.read_env(root / 'public.env')
    rows = json.loads(public['BOB_PUBLIC_BACKENDS'])
    assert [{k: row[k] for k in ('backend', 'kind', 'model', 'models', 'available')} for row in rows] == [
        dict(backend=setup['backend'], kind='cloud', model=setup['model'],
             models=[setup['model']], available=True)]
    # BU16: additive picker metadata; the five fields above are unchanged.
    assert rows[0]['choices'] == [dict(model=setup['model'], label=setup['model'], default=True)]
    assert rows[0]['billing'] in ('api_key', 'subscription') and rows[0]['provider_label']  # Kimi's key is a subscription
    assert set(rows[0]) == {'backend', 'kind', 'model', 'models', 'available',
                            'provider_label', 'choices', 'tool_capable', 'billing'}
    assert public['BOB_LANE_DEFAULT_BACKEND'] == setup['backend']
    assert model.status('fresh')['ready']
    assert model.status('fresh')['providers'][provider + '_key']
    assert r.calls == [('restart', 'bob-core@fresh.service')]
    assert gateway_env.read_bytes() == gateway_before
    assert not (root / 'providers.env').exists()
    assert receipt['stored'] and 'fake-provider-canary' not in json.dumps(receipt)
    for name, mode in [('lane.env', 0o600), ('core.env', 0o640), ('public.env', 0o644)]:
        path = root / name
        assert 'fake-provider-canary' not in path.read_text()
        assert_permissions(path, mode, gid=ids['service_gid'] if name == 'core.env' else 0)
    dropin = r.systemd / f'bob-settings-control@{ids["lane_id"]}.service.d/enrollment.conf'
    assert f'ReadWritePaths={root} ' in dropin.read_text()
    assert '\nReadWritePaths=/run\n' not in dropin.read_text()
    assert not (r.systemd / f'bob-settings-receipt@{ids["lane_id"]}.service.d/enrollment.conf').exists()
    assert (r.settings.TMPFILES / f'bob-settings-{ids["lane_id"]}.conf').is_file()
    # Dispatch resolves the new vault key; identical loaded envs need no restart.
    core_before = (root / 'core.env').read_bytes()
    rotated = r.settings.handle(cfg, ids['gateway_uid'], dict(version=1, op='secrets.set',
        provider=provider, key='rotated-provider-canary', request_key='rotation'))
    assert (root / 'core.env').read_bytes() == core_before
    assert r.calls == [('restart', 'bob-core@fresh.service')]
    assert r.settings.handle(cfg, ids['gateway_uid'], dict(version=1, op='status',
        operation_id=rotated['operation_id']))['phase'] == 'ready'
    assert gateway_env.read_bytes() == gateway_before


def test_upgrade_hook_repairs_enrollment_mounts_without_touching_credentials(runtime):
    r = runtime
    r.lane.add('fresh', 'fresh-password-canary')
    ids = r.worker.load_identity('fresh')
    home = r.settings.STATE / ids['lane_id'] / 'credentials'
    before = (home / 'active.json').read_bytes()
    dropin = r.systemd / f'bob-settings-control@{ids["lane_id"]}.service.d/enrollment.conf'
    dropin.unlink()
    spec = importlib.util.spec_from_file_location('upgrade_hooks_enrollment', ROOT / 'deploy/runtime/lib/upgrade_hooks.py')
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    hook = dict(hooks.HOOKS)['settings-enrollment']
    assert hook() is None and dropin.is_file()
    assert hook() is None and (home / 'active.json').read_bytes() == before


def assert_memory(r, name, ids, content=None):
    from core.starter_memory import STARTER_MEMORY
    if content is None:
        content = STARTER_MEMORY
    path = r.lane.FAMILY / name / 'MEMORY.md'
    assert_permissions(path, 0o640, ids['gateway_uid'], ids['service_gid'])
    assert path.read_text() == content


def test_fresh_lane_gets_gateway_writable_owner_memory(runtime):
    r = runtime
    r.lane.add('fresh', 'fresh-password-canary')
    ids = r.worker.load_identity('fresh')
    assert_memory(r, 'fresh', ids)
    # The folder stays root-owned, so no service account can replace the file.
    assert_permissions(r.lane.FAMILY / 'fresh', 0o750, 0, ids['service_gid'])


def test_lane_memory_repair_and_upgrade_hook_keep_existing_notes(runtime):
    r = runtime
    r.lane.add('retry', 'old-password-canary')
    root = r.lane.FAMILY / 'retry/bob-memory'
    ids = r.worker.load_identity('retry')
    memory = root.parent / 'MEMORY.md'
    # A file made by hand, or by an older shared-UID lane, belongs to another account.
    memory.write_text('- [note] keep me\n')
    os.chown(memory, ids['service_uid'], ids['service_gid'])
    memory.chmod(0o600)
    r.worker.lane_memory(root)
    assert_memory(r, 'retry', ids, '- [note] keep me\n')
    # Lanes provisioned before setup created the file get it from the upgrade hook.
    memory.unlink()
    spec = importlib.util.spec_from_file_location('upgrade_hooks_memory', ROOT / 'deploy/runtime/lib/upgrade_hooks.py')
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    hook = dict(hooks.HOOKS)['lane-memory']
    assert hook() is None
    assert_memory(r, 'retry', ids)
    memory.write_text('')
    assert hook() is None
    assert_memory(r, 'retry', ids)
    memory.write_text('- [note] second\n')
    assert hook() is None
    assert_memory(r, 'retry', ids, '- [note] second\n')


def test_lane_memory_refuses_symlink_and_hard_link(runtime):
    r = runtime
    r.lane.add('guard', 'guard-password-canary')
    root = r.lane.FAMILY / 'guard/bob-memory'
    memory = root.parent / 'MEMORY.md'
    target = r.base / 'elsewhere'
    target.write_text('not a lane file')
    target.chmod(0o600)
    for plant in (lambda: memory.symlink_to(target), lambda: os.link(target, memory)):
        memory.unlink()
        plant()
        with pytest.raises((OSError, ValueError)):
            r.worker.lane_memory(root)
        assert target.read_text() == 'not a lane file'
        assert_permissions(target, 0o600)


@pytest.mark.parametrize('owner_epoch', [None, 7])
def test_quiesced_migration_reads_relocated_sqlite_authority(runtime, owner_epoch):
    r = runtime
    root = r.lane.FAMILY / 'existing/bob-memory'
    root.mkdir(parents=True)
    values = r.lane.lane_values('existing', 7835, 7836, 8902,
                                'keep-password', 'keep-totp', 'keep-vouch')
    (root / 'lane.env').write_text(r.common.render_env(values))
    with sqlite3.connect(root / 'gateway.db') as db:
        db.execute('CREATE TABLE unrelated (value TEXT)')
        if owner_epoch is not None:
            db.execute('CREATE TABLE owner_epochs (lane TEXT, owner TEXT, epoch INTEGER)')
            lane_key = hashlib.sha256(str(root.resolve()).encode()).hexdigest()
            db.executemany('INSERT INTO owner_epochs VALUES (?,?,?)', [
                (lane_key, 'existing', owner_epoch), (lane_key, 'another-owner', 99)])
    assert not (r.base / 'etc/bob').exists()
    # Same quiesced provisioning entry point/flags used by migrate-workers.
    config = r.worker.provision('existing', root, activate=False, admit=False)
    ids = r.worker.load_identity('existing')
    assert config['admission'] == 'migration-pending'
    assert not (root / 'gateway.db').exists()
    assert (r.worker.GATEWAY_STATE / ids['lane_id'] / 'gateway.db').is_file()
    generation = json.loads((r.settings.STATE / ids['lane_id'] / 'credentials/initial.json').read_text())
    assert generation['epoch'] == (owner_epoch or 1)
    assert generation['password'] == values['BOBCLAW_PASSWORD']
    assert generation['totp_secret'] == values['TOTP_SECRET']
    assert not any(call[0] == 'enable' for call in r.calls)
    assert_layout(r, root, ids)


def test_reprovision_repairs_modes_and_env_without_rolling_back_password(runtime, monkeypatch):
    r = runtime
    r.lane.add('retry', 'old-password-canary')
    root = r.lane.FAMILY / 'retry/bob-memory'
    ids = r.worker.load_identity('retry')
    cfg = r.settings.config(ids['lane_id'])
    monkeypatch.setattr(r.credentials, 'ready', lambda cfg, epoch: True)
    result = r.settings.handle(cfg, ids['gateway_uid'], dict(version=1, op='password.change',
        expected_epoch=1, new='new-password-canary', request_key='once'))
    assert result == dict(changed=True, restarted=True)
    home = r.settings.STATE / ids['lane_id'] / 'credentials'
    active = (home / 'active.json').read_bytes()
    (home / 'current.env').write_text('interrupted publication')
    # Reproduce restrictive modes left by older root/077 provisioning, including
    # traversal parents and drop-ins; every retry must repair them explicitly.
    for path in [r.base / 'etc/bob', r.worker.CONFIG, r.settings.CONFIG, r.settings.STATE,
                 home.parent, home, *r.systemd.glob('*.d')]:
        path.chmod(0o700)
        os.chown(path, 0, 12345)
    r.worker.provision('retry', root, activate=False)
    assert (home / 'active.json').read_bytes() == active
    current = r.common.read_env(home / 'current.env', safe=True)
    assert current['BOBCLAW_PASSWORD'] == 'new-password-canary'
    assert current['BOB_CREDENTIAL_EPOCH'] == '2'
    assert r.common.read_env(root / 'lane.env')['BOBCLAW_PASSWORD'] == 'old-password-canary'
    assert_layout(r, root, ids)
    # Settings can repair its credential drop-in without assuming that worker
    # provisioning has already created the gateway's instance directory.
    dropins = r.systemd / 'bob-gateway@retry.service.d'
    for path in dropins.iterdir():
        path.unlink()
    dropins.rmdir()
    r.settings.provision(ids, root, activate=False)
    assert_permissions(dropins, 0o755)
    assert (dropins / 'zz-credentials.conf').is_file()
    assert (home / 'active.json').read_bytes() == active


def test_cc1_upgrade_hook_is_idempotent_and_keeps_existing_profiles_and_credentials(runtime):
    r = runtime
    r.lane.add('fresh', 'fresh-password-canary')
    root = r.lane.FAMILY / 'fresh/bob-memory'
    ids = r.worker.load_identity('fresh')
    config_path = r.worker.CONFIG / (ids['lane_id'] + '.json')
    before = json.loads(config_path.read_text())
    before['profiles'].pop('claude')
    before.pop('claude_home')
    config_path.write_text(json.dumps(before))
    credential = root / 'cli/claude/.credentials.json'
    credential.write_text('{"claudeAiOauth":{"accessToken":"FAKE-UNCHANGED"}}')
    os.chown(credential, ids['worker_uid'], ids['worker_gid'])
    credential.chmod(0o600)
    env_before = (root / 'lane.env').read_bytes()
    spec = importlib.util.spec_from_file_location('cc1_upgrade_hook', ROOT / 'deploy/runtime/lib/upgrade_hooks.py')
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    assert hooks.claude_setup('unknown') == []
    for _ in range(2):
        assert hooks.claude_setup('fresh') == [f'bob-worker@{ids["lane_id"]}.service']
        after = json.loads(config_path.read_text())
        assert {name: row for name, row in after['profiles'].items() if name != 'claude'} == before['profiles']
        assert (root / 'lane.env').read_bytes() == env_before
        assert credential.read_text() == '{"claudeAiOauth":{"accessToken":"FAKE-UNCHANGED"}}'
        assert_permissions(credential, 0o600, ids['worker_uid'], ids['worker_gid'])
    dropin = r.systemd / f'bob-worker@{ids["lane_id"]}.service.d/claude.conf'
    assert f'ReadWritePaths={root}/cli/claude' in dropin.read_text()
