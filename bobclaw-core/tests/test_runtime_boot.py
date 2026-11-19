"""Offline systemd ordering and acceptance preflight regressions."""
import configparser
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

RUNTIME = Path(__file__).resolve().parents[2] / 'deploy/runtime'


def unit(source):
    parsed = configparser.ConfigParser(interpolation=None, strict=False)
    parsed.read_string(source)
    return parsed


def words(parsed, section, key):
    return set(parsed.get(section, key, fallback='').split())


def defaults(parsed):
    return parsed.getboolean('Unit', 'DefaultDependencies', fallback=True)


def ordering(units):
    """Prerequisite edges, including the defaults responsible for this cycle class."""
    graph = {'basic.target': {'sockets.target'}, 'sockets.target': set()}
    for name, parsed in units.items():
        name = name.replace('@.', '@%i.')
        graph.setdefault(name, set()).update(words(parsed, 'Unit', 'After'))
        for later in words(parsed, 'Unit', 'Before'):
            graph.setdefault(later, set()).add(name)
        if defaults(parsed):
            if name.endswith('.service'):
                graph[name].add('basic.target')
            elif name.endswith('.socket'):
                graph['sockets.target'].add(name)
    return graph


def reaches(graph, start, target, seen=None):
    seen = set() if seen is None else seen
    if start == target:
        return True
    if start in seen:
        return False
    seen.add(start)
    return any(reaches(graph, dep, target, seen) for dep in graph.get(start, ()))


def late_socket_dependencies(units):
    graph = ordering(units)
    problems = []
    for name, parsed in units.items():
        if not name.endswith('.socket') or not defaults(parsed):
            continue
        dependencies = set().union(*(words(parsed, 'Unit', key) for key in ('Requires', 'After', 'BindsTo')))
        for dep in dependencies:
            if dep.endswith('.service') and reaches(graph, dep, 'basic.target'):
                problems.append((name, dep))
    return problems


@pytest.mark.parametrize('service', [
    '[Unit]\nDescription=default service\n',
    '[Unit]\nDefaultDependencies=no\nAfter=basic.target\n',
    '[Unit]\nDefaultDependencies=no\nAfter=late.target\n',
])
@pytest.mark.parametrize('dependency', ['Requires', 'After', 'BindsTo'])
def test_ordering_guard_detects_default_and_explicit_late_services(service, dependency):
    units = {'worker.socket': unit(f'[Unit]\n{dependency}=policy.service\n'),
             'policy.service': unit(service), 'late.target': unit('[Unit]\nAfter=basic.target\n')}
    assert late_socket_dependencies(units) == [('worker.socket', 'policy.service')]


def test_installed_units_have_no_late_default_socket_or_ordering_cycle():
    units = {path.name: unit(path.read_text()) for path in (RUNTIME / 'systemd').iterdir()
             if path.suffix in {'.service', '.socket', '.target', '.timer'}}
    assert not late_socket_dependencies(units)
    graph = ordering(units)
    for name, prerequisites in graph.items():
        assert not any(reaches(graph, dep, name) for dep in prerequisites), name
    sockets = {'bob-worker@%i.socket', 'bob-worker-auth@%i.socket'}
    # Template references use %i, filenames use the empty instance.
    for filename in ('bob-worker@.socket', 'bob-worker-auth@.socket'):
        parsed = units[filename]
        own = filename.replace('@.', '@%i.')
        assert not defaults(parsed)
        assert words(parsed, 'Install', 'WantedBy') == {'multi-user.target'}
        assert 'shutdown.target' in words(parsed, 'Unit', 'Before')
        assert 'shutdown.target' in words(parsed, 'Unit', 'Conflicts')
        assert 'bob-worker-policy@%i.service' in words(parsed, 'Unit', 'After')
        assert sockets - {own} <= words(parsed, 'Unit', 'BindsTo')
    for filename in ('bob-worker@.service', 'bob-worker-auth@.service'):
        assert sockets <= words(units[filename], 'Unit', 'BindsTo')
        assert sockets <= words(units[filename], 'Unit', 'After')
        assert units[filename].get('Service', 'SystemCallErrorNumber') == 'EPERM'
    assert units['bob-worker-policy@.service'].get('Service', 'NotifyAccess') == 'all'


def test_guard_rejects_original_worker_socket_defaults():
    units = {path.name: unit(path.read_text()) for path in (RUNTIME / 'systemd').glob('bob-worker*')}
    for name in ('bob-worker@.socket', 'bob-worker-auth@.socket'):
        units[name].remove_option('Unit', 'DefaultDependencies')
    assert len(late_socket_dependencies(units)) == 2
    graph = ordering(units)
    assert reaches(graph, 'bob-worker@%i.socket', 'bob-worker-policy@%i.service')
    assert reaches(graph, 'bob-worker-policy@%i.service', 'bob-worker@%i.socket')


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, 'path', sys.path.copy())
    spec = importlib.util.spec_from_file_location('runtime_acceptance', RUNTIME / 'tests/acceptance.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(module, 'FAMILY', tmp_path / 'family')
    monkeypatch.setattr(module, 'WORKER_CONFIGS', tmp_path / 'workers')
    release = tmp_path / 'osrelease'
    release.write_text('Linux')
    monkeypatch.setattr(module, 'OSRELEASE', release)
    monkeypatch.setenv('PATH', module.os.environ.get('PATH', ''))
    monkeypatch.setattr(module.shutil, 'which', lambda name: '/usr/bin/' + name)
    def validate_name(name):
        import re
        if re.fullmatch(r'[a-z][a-z0-9-]{0,31}', name) is None:
            raise ValueError('invalid lane name')
        return name
    monkeypatch.setitem(sys.modules, 'lane', SimpleNamespace(validate_name=validate_name))
    calls = []
    monkeypatch.setattr(module, 'admin', lambda *args, **kw: calls.append(args) or SimpleNamespace(returncode=0))
    monkeypatch.setattr(module, 'Harness', lambda *args: pytest.fail('must reject before modifying profiles/restarting services'))
    return module, calls


@pytest.mark.parametrize('endpoint', ['bad', 'localhost:123', '127.0.0.1:no', '127.0.0.1:0',
    '127.0.0.1:65536', '[::1', '127.0.0.1:3/path', 'user@127.0.0.1:3'])
def test_bad_listener_never_creates_lanes(acceptance, monkeypatch, endpoint):
    module, calls = acceptance
    monkeypatch.setattr(sys, 'argv', ['acceptance.py', '--create', '--host-listener', endpoint])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2 and not calls


@pytest.mark.parametrize('args', [
    ['--lane-b', 'INVALID'], ['--lane-b', 's2d-test-a'], ['--after-reboot'], ['--codex-positive'],
    ['--after-reboot', '--codex-positive'],
])
def test_invalid_lane_or_mode_never_creates_lanes(acceptance, monkeypatch, args):
    module, calls = acceptance
    monkeypatch.setattr(sys, 'argv', ['acceptance.py', '--create', *args])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2 and not calls


def test_existing_second_lane_rejected_before_first_created(acceptance, monkeypatch):
    module, calls = acceptance
    (module.FAMILY / 's2d-test-b').mkdir(parents=True)
    monkeypatch.setattr(sys, 'argv', ['acceptance.py', '--create'])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2 and not calls


@pytest.mark.parametrize('wsl,provided', [(False, True), (True, False)])
def test_dead_or_missing_wsl_listener_rejected_before_creation(acceptance, monkeypatch, wsl, provided):
    module, calls = acceptance
    module.OSRELEASE.write_text('microsoft' if wsl else 'Linux')
    class ClosedListener:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect_ex(self, endpoint): return 1
    monkeypatch.setattr(module.socket, 'socket', lambda *args: ClosedListener())
    monkeypatch.setattr(sys, 'argv', ['acceptance.py', '--create'] +
                        (['--host-listener', '127.0.0.1:123'] if provided else []))
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 2 and not calls


def test_numeric_ipv4_and_bracketed_ipv6_listeners(acceptance):
    module, _ = acceptance
    assert module.host_listener('192.0.2.1:123') == ('192.0.2.1', 123)
    assert module.host_listener('[2001:db8::1]:456') == ('2001:db8::1', 456)


@pytest.mark.parametrize('missing',['bob-worker-auth','bob-worker-journal'])
def test_boot_probe_covers_every_lane_and_detects_missing_socket(acceptance, monkeypatch, missing):
    module, calls = acceptance
    module.WORKER_CONFIGS.mkdir()
    for lane in ('s2d-test-a', 's2d-test-b', 'other-provisioned-lane'):
        (module.WORKER_CONFIGS / (lane + '.json')).write_text(json.dumps({'lane': lane}))
    monkeypatch.setattr(Path, 'is_file', lambda path: True)
    def admin(*args):
        calls.append(args)
        return SimpleNamespace(returncode=int(args[-1] == missing+'@other-provisioned-lane.socket'))
    monkeypatch.setattr(module, 'admin', admin)
    rows = module.boot_workers(timeout=0)
    assert len(rows) == 3 and len(calls) == 12
    assert [probe for probe, passed, _ in rows if not passed] == ['lifecycle.boot-workers.other-provisioned-lane']


def test_reboot_failure_is_reported_before_harness_can_repair_it(acceptance, monkeypatch):
    module, calls = acceptance
    for lane in ('s2d-test-a', 's2d-test-b'):
        root = module.FAMILY / lane / 'bob-memory'
        root.mkdir(parents=True)
        (root / 'acceptance-disposable.json').write_text('{"purpose":"s2d-disposable"}')
        (root / 'acceptance-reboot.json').write_text('{}')
    monkeypatch.setattr(module, 'boot_workers', lambda: [('lifecycle.boot-workers.lane', False, {})])
    monkeypatch.setattr(sys, 'argv', ['acceptance.py', '--after-reboot'])
    assert module.main() == 1 and not calls
