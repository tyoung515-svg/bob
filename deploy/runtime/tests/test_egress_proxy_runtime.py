"""Portable firewall/unit checks, plus real root-Linux provisioning when available."""
import ast
from configparser import ConfigParser
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUNTIME = ROOT / 'deploy/runtime'


@pytest.fixture
def tmp_path(tmp_path_factory):
    if os.name != 'nt':
        return tmp_path_factory.mktemp('egress-runtime')
    folder = ROOT / 'bobclaw-core/test-db' / ('egress-' + uuid.uuid4().hex)
    folder.mkdir(mode=0o777, parents=True)
    return folder


def load_functions(*names, **namespace):
    # pwd/root provisioning imports are unavailable on Windows; execute the
    # actual portable functions, without copying their implementation.
    tree = ast.parse((RUNTIME / 'lib/worker.py').read_text())
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in names]
    assert len(nodes) == len(names)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<runtime worker>', 'exec'), namespace)
    return SimpleNamespace(**{name: namespace[name] for name in names})


def test_worker_uid_only_accepts_exact_lane_loopback_proxy_port():
    runtime = load_functions('nft_rules')
    a = runtime.nft_rules({'worker_uid': 61001, 'lane_id': 'aaa', 'egress_port': 21004})
    b = runtime.nft_rules({'worker_uid': 61006, 'lane_id': 'bbb', 'egress_port': 21009})
    assert 'meta skuid 61001 jump worker' in a and 'table inet bob_w_aaa' in a
    assert 'ip daddr 127.0.0.1 tcp dport 21004 accept' in a
    worker_rules = a.split('chain worker {', 1)[1]
    assert worker_rules.count('accept') == 1 and 'reject' in worker_rules
    assert 'dport 443' not in a and 'dport 53' not in a and 'ip6' not in a
    assert '21004' not in b and '61001' not in b
    for port in (None, True, 80, 65536, '21004'):
        with pytest.raises(ValueError, match='not provisioned'):
            runtime.nft_rules({'worker_uid': 61001, 'lane_id': 'aaa', 'egress_port': port})


def test_proxy_unit_is_dedicated_hardened_and_has_no_provider_credentials():
    unit = RUNTIME / 'systemd/bob-egress-proxy@.service'
    parser = ConfigParser(interpolation=None)
    parser.read(unit)
    service = parser['Service']
    assert service['Type'] == 'notify' and service['NotifyAccess'] == 'main'
    assert service['User'] == service['Group'] == 'bob-unprovisioned'
    assert service['NoNewPrivileges'] == 'yes'
    assert service['ProtectSystem'] == 'strict' and service['ProtectHome'] == 'yes'
    assert service['CapabilityBoundingSet'] == service['AmbientCapabilities'] == ''
    assert service['KillMode'] == 'control-group'
    assert service['RuntimeDirectory'] == 'bob-egress-%i'
    assert 'core.builder.egress_proxy /etc/bob/workers/%i.json' in service['ExecStart']
    assert 'identity.conf' in parser['Unit']['AssertPathExists']
    assert 'EnvironmentFile' not in service and 'Environment' not in service
    policy = (RUNTIME / 'systemd/bob-worker-policy@.service').read_text()
    assert 'Before=bob-worker@%i.socket bob-worker@%i.service' in policy


def test_proxy_binding_peer_identity_and_readiness_contract():
    source = (ROOT / 'bobclaw-core/core/builder/egress_proxy.py').read_text()
    assert "asyncio.start_server(proxy.request, '127.0.0.1', config['egress_port']" in source
    assert 'socket.SO_PEERCRED' in source and 'uid != self.service_uid' in source
    assert "proxy = Proxy(config['service_uid'], max_timeout=config.get('max_timeout', 640))" in source
    assert "sock.sendall(b'READY=1')" in source
    worker = (RUNTIME / 'lib/worker.py').read_text()
    assert "User={ids[\"egress_user\"]}" in worker
    assert "Requires=bob-egress-proxy@{lane_id}.service" in worker
    assert "'egress_port': ids['egress_uid'] - 40000" in worker
    migration = (RUNTIME / 'lib/migration.py').read_text()
    assert "f'bob-egress-proxy@{key}.service'" in migration
    # The proxy must ship with the default policy data in the existing source archive.
    build = (RUNTIME / 'build.sh').read_text()
    assert 'bobclaw-core bobclaw-gateway portal-broker migrations' in build
    policy = json.loads((ROOT / 'bobclaw-core/core/builder/network_allowlist.json').read_text())
    assert {'pypi.org', 'registry.npmjs.org', 'huggingface.co', 'docs.python.org'} <= set(policy['allowlist'])


def test_provisioned_codex_and_mcp_profiles_use_proxy_without_opening_firewall():
    tree = ast.parse((RUNTIME / 'lib/worker.py').read_text())
    provision = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'provision')
    assignment = next(n for n in provision.body if isinstance(n, ast.Assign)
                      and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'config')
    ids = {'lane_id': 'aaa', 'egress_uid': 61004}
    config = eval(compile(ast.Expression(assignment.value), '<provision config>', 'eval'), {
        'ids': ids, 'run_root': Path('/run-root'), 'lane_id': 'aaa', 'journal_path': Path('/journal'),
        'worker_home': Path('/home'), 'root': Path('/lane'),
        'claude_home': Path('/lane/cli/claude')})
    for name in ('codex', 'claude', 'mcp', 'builder'):
        assert config['profiles'][name]['network'] is True
        assert config['profiles'][name]['egress'] == 'lane-proxy/1'
    assert all(not config['profiles'][p]['network'] for p in ('shell', 'python', 'git', 'checked', 'proposal'))
    defaults = json.loads((ROOT / 'bobclaw-core/core/builder/network_allowlist.json').read_text())
    assert set(defaults['profiles']['codex']) == {'api.openai.com', 'chatgpt.com', 'auth.openai.com'}
    assert defaults['profiles']['mcp'] == []
    assert not set(defaults['profiles']['codex']) & set(defaults['allowlist'])


def test_s1b_egress_identity_does_not_skip_profile_reprovisioning(tmp_path):
    identities = {'lane_id': 'aaa', 'gateway_uid': 61002, 'bridge_uid': 61003, 'egress_uid': 61004}
    config_path = tmp_path / 'aaa.json'
    runtime = load_functions('proxy_profiles_ready', CONFIG=tmp_path, json=json)
    assert not runtime.proxy_profiles_ready(identities)
    profiles = {name: {'network': True} for name in ('builder', 'codex', 'claude', 'mcp')}
    profiles['builder']['egress'] = 'lane-proxy/1'
    config_path.write_text(json.dumps({'profiles': profiles}))
    assert not runtime.proxy_profiles_ready(identities)
    tree = ast.parse((RUNTIME / 'lib/migration.py').read_text())
    plan = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'plan')
    for state in (None, {'phase': 'completed', 'activation_complete': True, 'settings_version': 1}):
        namespace = {'VERSION': 1, 'directory': lambda p: nullcontext(),
            'read_env': lambda p: {'BOB_WORKER_ID': 'aaa'},
            'lane': SimpleNamespace(validate_name=lambda n: None, FAMILY=tmp_path, lane_values=lambda *a: {}),
            'worker': SimpleNamespace(load_identity=lambda n: identities, proxy_profiles_ready=runtime.proxy_profiles_ready),
            'state_for': lambda n: state, 'pending': lambda n: False}
        exec(compile(ast.Module(body=[plan], type_ignores=[]), '<migration plan>', 'exec'), namespace)
        assert namespace['plan']('alpha')['phase'] == 'quiesce'
        for profile in profiles.values():
            profile['egress'] = 'lane-proxy/1'
        config_path.write_text(json.dumps({'profiles': profiles}))
        assert runtime.proxy_profiles_ready(identities)
        assert namespace['plan']('alpha')['phase'] == 'completed'
        profiles['codex'].pop('egress')
        config_path.write_text(json.dumps({'profiles': profiles}))
    profiles['codex'].update(egress='lane-proxy/1', network=False)
    config_path.write_text(json.dumps({'profiles': profiles}))
    assert not runtime.proxy_profiles_ready(identities)


def test_retained_egress_identity_allocation_and_reprovision(tmp_path):
    accounts, commands = {}, []
    def missing_uid(uid):
        for value in accounts.values():
            if value.pw_uid == uid:
                return value
        raise KeyError(uid)
    def account(name):
        if name not in accounts:
            raise KeyError(name)
        return accounts[name]
    def command(argv, **kwargs):
        commands.append(argv)
        if argv[0] == 'useradd':
            uid = int(argv[argv.index('--uid') + 1])
            accounts[argv[-1]] = SimpleNamespace(pw_uid=uid, pw_gid=uid,
                pw_gecos=argv[argv.index('--comment') + 1])
    def write(target, text, *args):
        target.write_text(text, encoding='utf-8', newline='\n')
    identities = tmp_path / 'identities'
    runtime = load_functions('unused_uid', 'identity', 'load_identity',
        IDENTITIES=identities, require_root=lambda: None,
        root_directory=lambda target, **kw: target.mkdir(mode=0o777, parents=True, exist_ok=True),
        private_replace=write, json=json, hashlib=hashlib,
        pwd=SimpleNamespace(getpwnam=account, getpwuid=missing_uid), subprocess=SimpleNamespace(run=command))
    first = runtime.identity('lane-one')
    assert len({first[role + '_uid'] for role in ('service', 'worker', 'gateway', 'bridge', 'egress')}) == 5
    assert first['egress_user'].startswith('bob-e-') and first['phase'] == 'ready'
    assert runtime.identity('lane-one') == first
    second = runtime.identity('lane-two')
    assert second['egress_uid'] != first['egress_uid']
    assert 1024 <= first['egress_uid'] - 40000 <= 65535
    assert len([c for c in commands if c[0] == 'useradd']) == 10


@pytest.mark.parametrize('journal', [False, True])
def test_existing_isolated_lane_requires_egress_upgrade_before_noop(tmp_path, journal):
    tree = ast.parse((RUNTIME / 'lib/migration.py').read_text())
    functions = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'plan', 'worker_units'}]
    ids = {'lane_id': 'abc', 'gateway_uid': 61002, 'bridge_uid': 61003}
    state = {'lane': 'alpha', 'version': 1, 'phase': 'completed', 'settings_version': 1,
             'activation_complete': True} if journal else None
    namespace = {'VERSION': 1, 'directory': lambda p: nullcontext(),
        'read_env': lambda p: {'BOB_WORKER_ID': 'abc'},
        'lane': SimpleNamespace(validate_name=lambda n: None, FAMILY=tmp_path, lane_values=lambda *args: {}),
        'worker': SimpleNamespace(load_identity=lambda n: ids, proxy_profiles_ready=lambda value: 'egress_uid' in value),
        'state_for': lambda n: state,
        'pending': lambda n: False}
    exec(compile(ast.Module(body=functions, type_ignores=[]), '<runtime migration>', 'exec'), namespace)
    result = namespace['plan']('alpha')
    assert result['phase'] == 'quiesce'
    if journal:
        assert result['egress_upgrade'] is True
    # Old installations have no proxy unit to stop before the templates install.
    assert not any('egress' in name for name in namespace['worker_units'](ids))
    ids['egress_uid'] = 61004
    assert namespace['plan']('alpha')['phase'] == 'completed'
    assert 'bob-egress-proxy@abc.service' in namespace['worker_units'](ids)


@pytest.mark.skipif(os.name != 'posix' or getattr(os, 'geteuid', lambda: -1)() != 0,
                    reason='requires real root Linux provisioning syscalls')
def test_real_provision_writes_proxy_identity_dependency_and_activation(request):
    # Reuse the established real-files fixture, not a second provisioning mock.
    runtime = request.getfixturevalue('runtime')
    runtime.lane.add('network', 'fixture-password')
    ids = runtime.worker.load_identity('network')
    config = json.loads((runtime.worker.CONFIG / (ids['lane_id'] + '.json')).read_text())
    proxy = runtime.systemd / ('bob-egress-proxy@' + ids['lane_id'] + '.service.d/identity.conf')
    assert 'User=' + ids['egress_user'] in proxy.read_text()
    dependency = runtime.systemd / 'bob-core@network.service.d/egress.conf'
    assert 'Requires=bob-egress-proxy@' + ids['lane_id'] + '.service' in dependency.read_text()
    assert config['egress_port'] == ids['egress_uid'] - 40000
    assert config['profiles']['builder']['egress'] == 'lane-proxy/1'
    assert all(config['profiles'][p]['egress'] == 'lane-proxy/1' for p in ('codex', 'claude', 'mcp'))
    assert config['profiles']['checked']['network'] is False
    assert any('bob-egress-proxy@' + ids['lane_id'] + '.service' in call for call in runtime.calls)


# Imported fixture is only evaluated by the explicitly Linux-only test above.
from test_provisioning import runtime  # noqa: E402,F401
