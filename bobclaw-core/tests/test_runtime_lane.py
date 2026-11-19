"""Runtime lane planning contracts; these tests do not need sockets or systemd."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import runpy
import sys
import tomllib
import time
from types import SimpleNamespace

import jwt
import pytest

RUNTIME = Path(__file__).resolve().parents[2] / 'deploy' / 'runtime' / 'lib'


@pytest.fixture(scope='module')
def runtime():
    # Use unique module names and restore sys.modules, avoiding application imports.
    saved = {key: sys.modules.get(key) for key in ('common', '_runtime_lane_test')}
    try:
        for name, filename in [('common', 'common.py'), ('_runtime_lane_test', 'lane.py')]:
            spec = importlib.util.spec_from_file_location(name, RUNTIME / filename)
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        yield sys.modules['_runtime_lane_test']
    finally:
        for key, module in saved.items():
            if module is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = module


@pytest.mark.parametrize('name', ['a', 'owner', 'luna-2', 'a' * 32, 'a-'])
def test_valid_names(runtime, name):
    assert runtime.validate_name(name) == name


@pytest.mark.parametrize('name', ['', 'A', '2dad', '-dad', '../dad', 'a/b', 'dad_2',
                                 'a' * 33, 'dad\n', 'é', 'a b', 'a@b'])
def test_invalid_names(runtime, name):
    with pytest.raises(ValueError):
        runtime.validate_name(name)


@pytest.mark.parametrize('used,expected', [
    (set(), (7835, 7836)), ({7835}, (7837, 7838)), ({7836}, (7837, 7838)),
    ({7835, 7836, 7838}, (7839, 7840)), ({7837, 7838}, (7835, 7836)),
    ({8902, 5432, 6379, 6353, 6354, 58081}, (7835, 7836)),
])
def test_next_free_pair(runtime, used, expected):
    original = used.copy()
    assert runtime.allocate_ports(used) == expected
    assert used == original


def test_exhaustion_and_last_pair(runtime):
    used = set(range(7835, 65535))
    used.difference_update({65533, 65534})
    assert runtime.allocate_ports(used) == (65533, 65534)
    used.add(65534)
    with pytest.raises(ValueError):
        runtime.allocate_ports(used)


def test_bridge_respects_other_services(runtime):
    assert runtime.allocate_bridge({8902, 8903, 8905}) == 8904
    with pytest.raises(ValueError):
        runtime.allocate_bridge(set(range(8902, 65536)))


def test_environment_and_store_isolation(runtime, tmp_path):
    a = runtime.lane_values('owner', 7835, 7836, 8902, 'safe password', 'TOTP1', 'SECRET1')
    b = runtime.lane_values('luna-2', 7837, 7838, 8903, 'other password', 'TOTP2', 'SECRET2')
    for key in ('MEMORY_SQLITE_PATH', 'GATEWAY_SQLITE_PATH', 'BOBCLAW_LEDGER_INSTANCES',
                'MEMORY_STORES_CONFIG_PATH', 'CC_PROJECT_DIR', 'CC_SCRATCH_ROOT',
                'AGY_PROJECT_DIR', 'AGY_SCRATCH_ROOT', 'CODEX_PROJECT_DIR', 'CODEX_SCRATCH_ROOT',
                'CC_SIDECAR_PATH', 'CODEX_HOME', 'CC_CONFIG_DIR', 'AGY_HOME'):
        assert a[key] != b[key]
        assert a[key].startswith('/var/lib/bob/family/owner/bob-memory/')
        assert b[key].startswith('/var/lib/bob/family/luna-2/bob-memory/')
    assert a['CORE_URL'] == a['BOB_BRIDGE_ENGINE'] == 'http://127.0.0.1:7835'
    assert b['BOBCLAW_GATEWAY_URL'] == 'http://127.0.0.1:7838'
    assert a['BOBCLAW_SECRET'] == a['JWT_SECRET'] == 'SECRET1'
    assert a['MEMORY_QDRANT_URL'] == 'http://127.0.0.1:6353'
    assert a['MEMORY_WRITE_FENCE_ENABLED'] == '1'
    for key in ('MEMORY_ENABLED', 'MEMORY_WRITER_ENABLED', 'MEMORY_T0_RECALL_ENABLED'):
        assert a[key] == 'true'
    for key in ('MEMORY_L1_EXTRACTION_ENABLED', 'MEMORY_LKS_FIRST', 'MEMORY_SINGLE_QDRANT'):
        assert a[key] == 'false'
    assert a['BOB_DEFAULT_OWNER'] == 'owner'
    stores = [tomllib.loads(runtime.render_stores(name)) for name in ('owner', 'luna-2')]
    assert stores[0]['stores']['owner_bob']['acl_allowed_providers'] == ['qdrant_local']
    assert 'luna-2_bob' in stores[1]['stores']
    prefixes = [s['providers']['qdrant_local']['collection_prefix'] for s in stores]
    assert len(set(prefixes)) == 2
    path = tmp_path / 'lane.env'
    path.write_text(runtime.render_env(a))
    assert runtime.read_env(path) == a


@pytest.mark.parametrize('password', ['hello world', 'a"b\\c', "a'b # c", '$HOME ${x} %i `id` $(id)', ' leading trailing '])
def test_env_quotes_preserve_password(runtime, tmp_path, password):
    path = tmp_path / 'lane.env'
    path.write_text(runtime.render_env({'BOBCLAW_PASSWORD': password}))
    assert runtime.read_env(path)['BOBCLAW_PASSWORD'] == password


@pytest.mark.parametrize('value', ['a\nb', 'a\rb', 'a\x00b', 'a\tb', 'a\x7fb'])
def test_env_rejects_control_injection(runtime, value):
    with pytest.raises(ValueError):
        runtime.render_env({'BOBCLAW_PASSWORD': value})


def test_retained_lanes_reserve_all_ports(runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, 'FAMILY', tmp_path)
    root = tmp_path / 'owner' / 'bob-memory'
    root.mkdir(parents=True)
    (root / '.disabled').touch()
    (root / 'lane.env').write_text(runtime.render_env({
        'BOBCLAW_CORE_PORT': '7835', 'BOBCLAW_GATEWAY_PORT': '7836', 'BOB_BRIDGE_PORT': '8902'}))
    assert runtime.allocate_ports(runtime.reserved_ports()) == (7837, 7838)
    assert runtime.allocate_bridge(runtime.reserved_ports()) == 8903


def test_env_private_and_exclusive(runtime, tmp_path):
    common = sys.modules['common']
    path = tmp_path / 'lane.env'
    common.private_write(path, 'test', os.getuid(), os.getgid())
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        common.private_write(path, 'overwrite', os.getuid(), os.getgid())
    target = tmp_path / 'symlink'
    target.symlink_to(path)
    with pytest.raises(FileExistsError):
        common.private_write(target, 'overwrite', os.getuid(), os.getgid())
    assert path.read_text() == 'test'


@pytest.fixture
def lane_install(runtime, monkeypatch, tmp_path):
    """Use the real gateway mint function in an isolated subprocess, no services."""
    data = tmp_path / 'data'
    (data / 'secrets').mkdir(parents=True)
    (data / 'family').mkdir()
    (data / '.firstboot-complete').touch()
    (data / 'secrets/bobclaw.env').write_text(runtime.render_env({
        'JWT_SECRET': 'shared-secret-must-not-sign-lane-tokens', 'PORTAL_TOKEN_DAYS': '7'}))
    monkeypatch.setattr(runtime, 'DATA', data)
    monkeypatch.setattr(runtime, 'FAMILY', data / 'family')
    monkeypatch.setattr(runtime, 'PORTAL', tmp_path / 'bob-portal')
    monkeypatch.setattr(runtime, 'SOURCE', RUNTIME.parents[2])
    monkeypatch.setattr(runtime, 'port_available', lambda port: True)
    uid, gid = os.getuid(), os.getgid()
    accounts = {'bob': SimpleNamespace(pw_uid=uid, pw_gid=gid),
                'bobapp': SimpleNamespace(pw_uid=uid + 10000, pw_gid=gid + 10000)}
    monkeypatch.setattr(runtime.pwd, 'getpwnam', accounts.__getitem__)
    # Record ownership requests without requiring root or real runtime accounts.
    owners = {}
    def record_owner(stat, owner, group):
        owners[stat.st_dev, stat.st_ino] = (owner, group)
    monkeypatch.setattr(runtime.os, 'chown', lambda path, owner, group:
                        record_owner(Path(path).stat(), owner, group))
    monkeypatch.setattr(runtime.os, 'fchown', lambda fd, owner, group:
                        record_owner(os.fstat(fd), owner, group))
    monkeypatch.setattr(runtime, 'test_owners', owners, raising=False)
    activations = []
    monkeypatch.setattr(runtime, 'systemctl', lambda *args: activations.append(args))
    monkeypatch.setenv('JWT_SECRET', 'admin-secret-must-not-sign-lane-tokens')
    monkeypatch.setenv('AGENT_TOKEN_DAYS', '99')
    monkeypatch.setenv('PYTHONPATH', '/not-the-gateway')
    return data, activations


def test_add_mints_and_rotate_replaces_private_lane_token(runtime, lane_install, capsys):
    data, activations = lane_install
    tokens = []

    def claims(name):
        root = data / 'family' / name / 'bob-memory'
        token_file = runtime.PORTAL / f'{name}.token'
        token = token_file.read_text().strip()
        tokens.append(token)
        env = runtime.read_env(root / 'lane.env')
        payload = jwt.decode(token, env['JWT_SECRET'], algorithms=['HS256'])
        assert payload['sub'] == name
        assert payload['token_type'] == 'portal'
        assert 'faces' not in payload  # portal faces are unrestricted
        assert payload['exp'] - payload['iat'] == 7 * 86400
        assert token_file.stat().st_mode & 0o777 == 0o600
        metadata = runtime.PORTAL / f'{name}.json'
        assert json.loads(metadata.read_text()) == {'gateway_port': int(env['BOBCLAW_GATEWAY_PORT'])}
        for path, mode in [(token_file, 0o600), (metadata, 0o600), (runtime.PORTAL, 0o700)]:
            stat = path.stat()
            assert stat.st_mode & 0o777 == mode
            assert runtime.test_owners[stat.st_dev, stat.st_ino] == (os.getuid() + 10000, os.getgid() + 10000)
        assert root.stat().st_mode & 0o777 == 0o700
        assert root.parent.stat().st_mode & 0o777 == 0o700
        assert not (root / 'broker.token').exists()
        assert not list(runtime.PORTAL.glob('.*'))
        for wrong_secret in ('shared-secret-must-not-sign-lane-tokens',
                             'admin-secret-must-not-sign-lane-tokens'):
            with pytest.raises(jwt.InvalidSignatureError):
                jwt.decode(token, wrong_secret, algorithms=['HS256'])
        return payload

    runtime.add('owner', 'strong test password')
    home = data / 'family/owner/bob-memory/codex-home'
    assert home.stat().st_mode & 0o777 == 0o700
    assert home.stat().st_uid == os.getuid()
    profiles = tomllib.loads((home / 'config.toml').read_text())['profiles']
    assert set(profiles) == {'gpt'}
    assert all(p['model_provider'] == 'openai' for p in profiles.values())
    first = claims('owner')
    assert first['scope'] == {
        'may_touch': [f'{data}/family/owner/**', f'{data}/family/shared/**'],
        'may_not_touch': [],
    }
    runtime.add('luna-2', 'another strong test password')
    other = claims('luna-2')
    assert other['scope'] == {
        'may_touch': [f'{data}/family/luna-2/**', f'{data}/family/shared/**'],
        'may_not_touch': [f'{data}/family/owner/**'],
    }
    # Retained lanes are also excluded when re-minting.
    (data / 'family/luna-2/bob-memory/.disabled').touch()
    legacy = data / 'family/owner/bob-memory/broker.token'
    legacy.write_text('legacy-lane-bearer')
    old_token = (runtime.PORTAL / 'owner.token').stat().st_ino
    old_metadata = (runtime.PORTAL / 'owner.json').stat().st_ino
    runtime.rotate_token('owner')
    assert not legacy.exists()
    assert (runtime.PORTAL / 'owner.token').stat().st_ino != old_token
    assert (runtime.PORTAL / 'owner.json').stat().st_ino != old_metadata
    rotated = claims('owner')
    assert rotated['scope'] == {
        'may_touch': [f'{data}/family/owner/**', f'{data}/family/shared/**'],
        'may_not_touch': [f'{data}/family/luna-2/**'],
    }
    assert rotated['jti'] != first['jti']
    assert claims('luna-2')['jti'] == other['jti']
    assert activations == [('enable', '--now', *runtime.units(name)) for name in ('owner', 'luna-2')]
    output = capsys.readouterr()
    assert all(token not in output.out + output.err for token in tokens)


@pytest.mark.parametrize('remaining,due', [(7 * 86400 + 1, False), (7 * 86400, False),
                                          (7 * 86400 - 1, True), (0, True), (-1, True)])
def test_token_renewal_threshold(runtime, remaining, due):
    now = time.time()
    token = jwt.encode({'sub': 'owner', 'token_type': 'portal', 'exp': now + remaining},
                       'lane-secret', algorithm='HS256')
    assert runtime.token_due(token, 'lane-secret', 'owner', now) is due


def test_renew_preserves_fresh_and_replaces_due_or_agent(runtime, lane_install, monkeypatch):
    data, _ = lane_install
    runtime.add('owner', 'strong test password')
    root = data / 'family/owner/bob-memory'
    path = runtime.PORTAL / 'owner.token'
    original = path.read_text()
    payload = jwt.decode(original.strip(), options={'verify_signature': False})
    monkeypatch.setattr(runtime.time, 'time', lambda: payload['iat'])
    assert not runtime.renew_if_due('owner')
    assert path.read_text() == original
    monkeypatch.setattr(runtime.time, 'time', lambda: payload['iat'] + 1)
    assert runtime.renew_if_due('owner')  # 7 day TTL now has <7 days left
    assert path.read_text() != original
    secret = runtime.read_env(root / 'lane.env')['JWT_SECRET']
    path.write_text(jwt.encode({'sub': 'owner', 'exp': payload['exp'] + 1000000,
                               'token_type': 'agent'}, secret, algorithm='HS256'))
    assert runtime.renew_if_due('owner')
    assert jwt.decode(path.read_text().strip(), secret, algorithms=['HS256'])['token_type'] == 'portal'


@pytest.mark.parametrize('state', ['legacy-only', 'fresh-with-legacy', 'missing-metadata', 'missing-token'])
def test_renew_migrates_legacy_and_repairs_missing_portal_files(runtime, lane_install, monkeypatch, state):
    data, _ = lane_install
    runtime.add('owner', 'strong test password')
    root = data / 'family/owner/bob-memory'
    token = runtime.PORTAL / 'owner.token'
    metadata = runtime.PORTAL / 'owner.json'
    original = token.read_text()
    payload = jwt.decode(original.strip(), options={'verify_signature': False})
    monkeypatch.setattr(runtime.time, 'time', lambda: payload['iat'])
    if state in {'legacy-only', 'fresh-with-legacy'}:
        (root / 'broker.token').write_text(original)
    if state in {'legacy-only', 'missing-token'}:
        token.unlink()
    if state in {'legacy-only', 'missing-metadata'}:
        metadata.unlink()
    assert runtime.renew_if_due('owner')
    assert token.read_text() != original
    assert json.loads(metadata.read_text()) == {'gateway_port': 7836}
    assert not (root / 'broker.token').exists()
    assert not list(runtime.PORTAL.glob('.*'))


def test_rotate_unlinks_legacy_symlink_without_touching_target(runtime, lane_install, tmp_path):
    data, _ = lane_install
    runtime.add('owner', 'strong test password')
    target = tmp_path / 'keep'
    target.write_text('must survive')
    legacy = data / 'family/owner/bob-memory/broker.token'
    legacy.symlink_to(target)
    runtime.rotate_token('owner')
    assert not legacy.is_symlink()
    assert target.read_text() == 'must survive'


@pytest.mark.parametrize('missing', [False, True])
def test_remove_deletes_portal_files_and_retains_lane_data(runtime, lane_install, monkeypatch, tmp_path, missing):
    data, activations = lane_install
    runtime.add('owner', 'strong test password')
    runtime.add('luna', 'another strong test password')
    root = data / 'family/owner/bob-memory'
    retained = (root / 'lane.env').read_bytes()
    other_token = (runtime.PORTAL / 'luna.token').read_bytes()
    (root / 'broker.token').write_text('legacy-lane-bearer')
    if missing:
        for suffix in ('token', 'json'):
            (runtime.PORTAL / f'owner.{suffix}').unlink()
    monkeypatch.setattr(runtime, 'require_root', lambda: None)
    monkeypatch.setattr(runtime, 'LOCK_PATH', str(tmp_path / 'lane.lock'))
    monkeypatch.setattr(sys, 'argv', ['bob-lane', 'remove', 'owner', '--keep-data'])
    assert runtime.main() == 0
    assert activations[-1] == ('disable', '--now', *reversed(runtime.units('owner')))
    assert not (runtime.PORTAL / 'owner.token').exists()
    assert not (runtime.PORTAL / 'owner.json').exists()
    assert not (root / 'broker.token').exists()
    assert (root / 'lane.env').read_bytes() == retained
    assert (root / '.disabled').is_file()
    assert (runtime.PORTAL / 'luna.token').read_bytes() == other_token
    assert (runtime.PORTAL / 'luna.json').is_file()


def test_renew_timer_only_invokes_active_lanes(runtime, lane_install, monkeypatch):
    data, _ = lane_install
    runtime.add('owner', 'strong test password')
    runtime.add('luna', 'another strong test password')
    (data / 'family/luna/bob-memory/.disabled').touch()
    monkeypatch.setitem(sys.modules, 'lane', runtime)
    spec = importlib.util.spec_from_file_location('_runtime_renew_test', RUNTIME / 'renew.py')
    renew = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renew)
    monkeypatch.setattr(renew, 'require_root', lambda: None)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(renew.subprocess, 'run', run)
    assert renew.main() == 0
    assert calls == [(['/usr/local/bin/bob-lane', 'renew-if-due', 'owner'], {'timeout': 60})]


def test_token_due_rejects_invalid_identity_and_signature(runtime):
    assert runtime.token_due('broken', 'secret', 'owner', 1)
    token = jwt.encode({'sub': 'other', 'exp': 9999999999, 'token_type': 'portal'},
                       'secret', algorithm='HS256')
    assert runtime.token_due(token, 'secret', 'owner', 1)
    assert runtime.token_due(token, 'wrong-secret', 'other', 1)


def test_rotate_failure_preserves_existing_token(runtime, lane_install, monkeypatch, capsys):
    data, _ = lane_install
    runtime.add('owner', 'strong test password')
    root = data / 'family/owner/bob-memory'
    path = runtime.PORTAL / 'owner.token'
    metadata = runtime.PORTAL / 'owner.json'
    previous = path.read_bytes(), metadata.read_bytes()
    (root / 'broker.token').write_text('legacy-lane-bearer')
    monkeypatch.setattr(runtime.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(
        returncode=1, stdout='secret-bearing output', stderr='secret-bearing traceback'))
    with pytest.raises(ValueError, match='gateway output withheld'):
        runtime.rotate_token('owner')
    assert (path.read_bytes(), metadata.read_bytes()) == previous
    assert (root / 'broker.token').read_text() == 'legacy-lane-bearer'
    assert not list(runtime.PORTAL.glob('.*'))
    output = capsys.readouterr()
    assert 'secret-bearing' not in output.out + output.err


def test_rotate_unknown_or_missing_lane_secret(runtime, lane_install):
    data, _ = lane_install
    with pytest.raises(ValueError, match='unknown lane'):
        runtime.rotate_token('unknown')
    root = data / 'family/owner/bob-memory'
    root.mkdir(parents=True)
    (root / 'lane.env').write_text('BOBCLAW_GATEWAY_PORT="7836"\n')
    with pytest.raises(ValueError, match='own JWT_SECRET'):
        runtime.rotate_token('owner')
    assert not (root / 'broker.token').exists()
    assert not runtime.PORTAL.exists()


def test_rotate_command(runtime, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, 'argv', ['bob-lane', 'rotate-token', 'owner'])
    monkeypatch.setattr(runtime, 'require_root', lambda: None)
    monkeypatch.setattr(runtime, 'open', lambda *args: open(tmp_path / 'lane.lock', 'a'), raising=False)
    names = []
    monkeypatch.setattr(runtime, 'rotate_token', names.append)
    assert runtime.main() == 0
    assert names == ['owner']
    assert capsys.readouterr().out == 'Rotated broker token for owner\n'


@pytest.fixture
def broker(runtime, monkeypatch):
    monkeypatch.setitem(sys.modules, 'lane', runtime)
    spec = importlib.util.spec_from_file_location('_runtime_broker_test', RUNTIME / 'broker.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('name', ['', '../dad', 'a/b', 'a;id', '$(id)', 'dad\n', 'A', 'a' * 33])
def test_broker_refuses_bad_lane_before_reading_files(broker, monkeypatch, name):
    def unexpected_read(path):
        pytest.fail('invalid lane caused a file read')
    monkeypatch.setattr(Path, 'read_text', unexpected_read)
    with pytest.raises(ValueError, match='lane name'):
        broker.broker_argv(name)


def test_broker_exact_exec_argv(broker, monkeypatch, tmp_path):
    monkeypatch.setattr(broker, 'PORTAL', tmp_path)
    (tmp_path / 'luna-2.json').write_text('{"gateway_port": 7840}\n')
    expected = ['/opt/bob/.venv/bin/python', '-u', '/opt/bob/portal-broker/portal-broker.py',
                '--gateway', 'http://127.0.0.1:7840', '--token-file', str(tmp_path / 'luna-2.token'),
                '--portal-approvals']
    assert broker.broker_argv('luna-2') == expected
    monkeypatch.setattr(sys, 'argv', ['bob-broker', 'luna-2'])
    accounts = {'bobapp': SimpleNamespace(pw_uid=1234), 'bob': SimpleNamespace(pw_uid=2345)}
    monkeypatch.setattr(broker.pwd, 'getpwnam', accounts.__getitem__)
    monkeypatch.setattr(broker.os, 'getuid', lambda: 1234)
    monkeypatch.setattr(broker.os, 'geteuid', lambda: 1234)
    calls = []
    monkeypatch.setattr(broker.os, 'execv', lambda *args: calls.append(args))
    assert broker.main() == 0
    assert calls == [(expected[0], expected)]


@pytest.mark.parametrize('uid,euid', [(0, 0), (2345, 2345), (3456, 3456),
                                    (1234, 0), (0, 1234), (1234, 2345), (2345, 1234)])
def test_broker_only_runs_as_bobapp(broker, monkeypatch, uid, euid):
    monkeypatch.setattr(sys, 'argv', ['bob-broker', 'owner'])
    accounts = {'bobapp': SimpleNamespace(pw_uid=1234), 'bob': SimpleNamespace(pw_uid=2345)}
    monkeypatch.setattr(broker.pwd, 'getpwnam', accounts.__getitem__)
    monkeypatch.setattr(broker.os, 'getuid', lambda: uid)
    monkeypatch.setattr(broker.os, 'geteuid', lambda: euid)
    with pytest.raises(ValueError, match='as bobapp.*root and bob are not allowed'):
        broker.main()


@pytest.mark.parametrize('port', [0, 65536, -1, True, None, 7836.0, '7836', 'abc',
                                 '7836/path', '７８３６', '7836\n'])
def test_broker_refuses_invalid_gateway_port(broker, monkeypatch, tmp_path, port):
    monkeypatch.setattr(broker, 'PORTAL', tmp_path)
    (tmp_path / 'owner.json').write_text(json.dumps({'gateway_port': port}))
    with pytest.raises(ValueError, match='gateway port'):
        broker.broker_argv('owner')


@pytest.mark.parametrize('override', [None, '/tmp/lane-broker.token'])
def test_portal_broker_default_token_path(monkeypatch, override):
    if override is None:
        monkeypatch.delenv('BOB_BROKER_TOKEN_FILE', raising=False)
    else:
        monkeypatch.setenv('BOB_BROKER_TOKEN_FILE', override)
    module = runpy.run_path(str(RUNTIME.parents[2] / 'portal-broker/portal-broker.py'))
    assert module['DEFAULT_TOKEN_FILE'] == override
    assert module['VERSION'] == '1.3.0'


def test_firstboot_migration_order_resume_and_changed_file(runtime, monkeypatch, tmp_path, capsys):
    """Exercise the orchestration against a fake ledger, without claiming SQL execution."""
    from types import SimpleNamespace
    import re

    spec = importlib.util.spec_from_file_location('_runtime_firstboot_test', RUNTIME / 'firstboot.py')
    firstboot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(firstboot)
    data = tmp_path / 'data'
    (data / 'secrets').mkdir(parents=True)
    password = 'a' * 43
    (data / 'secrets/bobclaw.env').write_text(runtime.render_env({
        'POSTGRES_PASSWORD': password, 'POSTGRES_URL': 'postgresql://old', 'BOBCLAW_PASSWORD': 'change-me',
        'TOTP_SECRET': 'old', 'DEEPSEEK_API_KEY': 'retained-provider-key'}))
    source = tmp_path / 'source'
    (source / 'migrations').mkdir(parents=True)
    (source / 'init.sql').write_text('-- init\n')
    for name in ('02_b.sql', '01_a.sql'):
        (source / 'migrations' / name).write_text(f'-- {name}\n')
    monkeypatch.setattr(firstboot, 'DATA', data)
    monkeypatch.setattr(firstboot, 'SOURCE', source)
    monkeypatch.setattr(firstboot, 'require_root', lambda: None)
    monkeypatch.setattr(firstboot, 'open', lambda *args: open(tmp_path / 'firstboot.lock', 'a'), raising=False)
    monkeypatch.setattr(firstboot.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid()))
    ledger, applied, statements = {}, [], []

    def fake_psql(sql, database='postgres'):
        statements.append(sql)
        if 'pg_database' in sql:
            return '1'
        if sql.startswith('SELECT sha256'):
            name = re.search("filename='([^']+)'", sql)[1]
            return ledger.get(name, '')
        if sql.startswith('BEGIN;'):
            assert sql.endswith('COMMIT;')
            name, digest = re.search(r"VALUES \('([^']+)', '([0-9a-f]{64})'\)", sql).groups()
            assert 'SET LOCAL ROLE bobclaw;' in sql
            ledger[name] = digest
            applied.append(name)
        return ''

    monkeypatch.setattr(firstboot, 'psql', fake_psql)
    previous_umask = os.umask(0o077)
    try:
        assert firstboot.main() == 0
        assert applied == ['init.sql', 'migrations/01_a.sql', 'migrations/02_b.sql']
        env = runtime.read_env(data / 'secrets/bobclaw.env')
        assert env['POSTGRES_URL'] == f'postgresql://bobclaw:{password}@127.0.0.1:5432/bobclaw'
        assert env['DEEPSEEK_API_KEY'] == 'retained-provider-key'
        assert 'BOBCLAW_PASSWORD' not in env and 'TOTP_SECRET' not in env
        count = len(statements)
        assert firstboot.main() == 0
        assert len(statements) == count  # marker prevents repeated boot work
        (data / '.firstboot-complete').unlink()
        assert firstboot.main() == 0
        assert len(applied) == 3  # durable ledger prevents reapplying completed files
        (data / '.firstboot-complete').unlink()
        (source / 'migrations/01_a.sql').write_text('-- edited\n')
        with pytest.raises(ValueError, match='applied migration changed'):
            firstboot.main()
        assert not (data / '.firstboot-complete').exists()
    finally:
        os.umask(previous_umask)
    output = capsys.readouterr().out
    assert password not in output and 'retained-provider-key' not in output


@pytest.mark.parametrize('piped, expected', [
    ('correct horse battery\n', 'correct horse battery'),
    ('correct horse battery\r\n', 'correct horse battery'),  # Windows pipe into wsl.exe
    ('correct horse battery', 'correct horse battery'),
])
def test_password_stdin_drops_one_line_ending(runtime, monkeypatch, tmp_path, piped, expected):
    import io
    seen = []
    monkeypatch.setattr(runtime, 'require_root', lambda: None)
    monkeypatch.setattr(runtime, 'LOCK_PATH', str(tmp_path / 'bob-lane.lock'))
    monkeypatch.setattr(runtime, 'add', lambda name, password: seen.append((name, password)))
    monkeypatch.setattr(sys, 'stdin', io.StringIO(piped))
    monkeypatch.setattr(sys, 'argv', ['bob-lane', 'add', 'owner', '--password-stdin'])
    runtime.main()
    assert seen == [('owner', expected)]
