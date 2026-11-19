"""Hermetic boundary contracts. Kernel enforcement belongs to runtime acceptance."""
import ast
import asyncio
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import sys
from types import SimpleNamespace

import pytest

from core.worker import client
from core.worker.contract import (MAX_REQUEST, WorkerUnavailable, child_env, peer_lane,
                                  resolve_profile, validate)


@pytest.fixture(autouse=True)
def packaged_worker_mode(monkeypatch):
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'worker')


@pytest.fixture
def payload():
    return dict(version=1, job_id='unit_1', profile='shell', workspace='scratch', cwd='.',
                args=['-c', 'echo ok'], input='', timeout=10, output_limit=1024, expires=1050)


@pytest.mark.parametrize('field', ['env', 'executable', 'lane', 'uid', 'gid', 'mounts', 'fds',
                                  'capabilities', 'unit', 'socket', 'systemd_properties'])
def test_forbidden_request_fields(payload, field):
    with pytest.raises(ValueError):
        validate({**payload, field: 'forged'}, now=1000)


@pytest.mark.parametrize('key,value', [('version', True), ('profile', 'root-shell'), ('workspace', '../b'),
    ('job_id', ''), ('cwd', '../secrets'), ('cwd', '/etc'), ('cwd', 'a\\b'), ('input', '\0'),
    ('args', ['x\0y']), ('args', ['x' * 65537]), ('args', 'ls'), ('timeout', 901),
    ('timeout', float('nan')), ('output_limit', 0), ('output_limit', 1.5), ('expires', 999),
    ('expires', 3000), ('input', 'x' * MAX_REQUEST)])
def test_invalid_requests(payload, key, value):
    with pytest.raises((ValueError, TypeError)):
        validate({**payload, key: value}, now=1000)


def test_valid_request_and_peer_mapping(payload):
    assert validate(payload, now=1000) is payload
    cfg = {'lane_id': 'a', 'service_uid': 101}
    assert peer_lane(101, cfg) == 'a'
    for uid in (0, 102, 201):
        with pytest.raises(PermissionError):
            peer_lane(uid, cfg)


def test_environment_is_constructed_empty(monkeypatch):
    monkeypatch.setenv('JWT_SECRET', 'CANARY')
    monkeypatch.setenv('SSH_AUTH_SOCK', '/canary')
    monkeypatch.setenv('LD_PRELOAD', '/canary')
    monkeypatch.setenv('HTTP_PROXY', 'canary')
    env = child_env({'worker_home': '/worker', 'codex_home': '/grant'}, '/job')
    assert 'CANARY' not in str(env) and 'canary' not in str(env)
    assert env['HOME'] == '/job' and env['TMPDIR'] == '/job'
    assert 'CODEX_HOME' not in env and 'PYTHONPATH' not in env
    assert child_env({'worker_home': '/worker', 'codex_home': '/grant'}, '/job', codex=True)['CODEX_HOME'] == '/grant'


def test_installed_profile_and_registered_workspace(payload, tmp_path):
    cfg = {'profiles': {'shell': {'argv': ['/bin/sh'], 'network': False}},
           'workspaces': {'scratch': str(tmp_path)}}
    argv, cwd, network = resolve_profile(cfg, payload)
    assert argv == ['/bin/sh', '-c', 'echo ok'] and cwd == tmp_path and not network
    with pytest.raises(WorkerUnavailable, match='resolution refused'):
        resolve_profile(cfg, {**payload, 'workspace': 'foreign'})
    with pytest.raises(WorkerUnavailable):
        resolve_profile(cfg, {**payload, 'profile': 'codex'})


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink/UID/dirfd semantics')
def test_profile_symlink_escape(payload, tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(WorkerUnavailable, match='resolution refused'):
        resolve_profile({'profiles': {'shell': {'argv': ['/bin/sh']}}, 'workspaces': {'scratch': str(root)}},
                        {**payload, 'cwd': 'escape'})


def test_no_boundary_never_launches(monkeypatch):
    from core import lane_config
    monkeypatch.setattr(lane_config, 'get_lane', lambda: SimpleNamespace(values={}))
    with pytest.raises(WorkerUnavailable, match='migration'):
        client.run(['sh', '-c', 'echo should-not-run'])


@pytest.mark.parametrize('backend', sorted(client.EXCLUDED))
def test_excluded_routes_refused_even_in_roster_mode(backend):
    from core.lane_config import require_backend, LaneConfigurationError
    with pytest.raises(LaneConfigurationError, match='unavailable'):
        require_backend(backend)


def test_excluded_configured_rows_have_reason(tmp_path):
    from core.lane_config import read_lane
    (tmp_path / 'lane.env').write_text('BOB_LANE_BACKENDS="' + ','.join(sorted(client.EXCLUDED)) + '"\n')
    lane = read_lane(tmp_path)
    assert 'claude_code' not in lane.backends  # no sign-in/model, no empty binding
    for name in client.EXCLUDED - {'claude_code'}:
        row = lane.backends[name].row()
        assert not row['available'] and 'unavailable' in row['unavailable_reason']


def test_shared_auth_catalog_does_not_resolve_worker_planted_paths(tmp_path, monkeypatch):
    from core import lane_config as lc
    from core.worker import auth_file
    (tmp_path / 'lane.env').write_text('BOB_WORKER_ID="abc"\nBOB_WORKER_UID="123"\n'
        'BOB_LANE_BACKENDS="claude_code,agy_code"\n')
    monkeypatch.setattr(lc.LaneConfiguration, 'owned_path', lambda *args: pytest.fail('must not resolve worker paths before no-follow opens'))
    monkeypatch.setattr(auth_file, 'read', lambda *args: {'tokens': {'access_token': 'synthetic'}})
    monkeypatch.setattr(auth_file, 'configuration_text', lambda *args: '[profiles.gpt]\nmodel_provider="openai"\n')
    monkeypatch.setattr(lc.shutil, 'which', lambda binary: '/installed/codex')
    monkeypatch.setattr(lc, 'unavailable_reason_for_lane', lambda lane: None)
    lane = lc.read_lane(tmp_path)
    assert lane.backends['codex_code'].available and lane.backends['openai_codex'].available
    assert 'claude_code' not in lane.backends and not lane.backends['agy_code'].available


def test_generic_adapter_does_not_transmit_env(monkeypatch, tmp_path):
    cfg = {'workspaces': {'scratch': str(tmp_path)}}
    monkeypatch.setattr(client, 'configuration', lambda: cfg)
    receipts = []
    def exchange(payload):
        receipts.append(payload)
        return {'returncode': 0, 'stdout': b'ok', 'stderr': b'', 'killed': False}
    monkeypatch.setattr(client, 'exchange', exchange)
    assert client.run(['echo', 'ok'], cwd=tmp_path, env={'JWT_SECRET': 'canary'}, text=True).stdout == 'ok'
    assert 'canary' not in json.dumps(receipts) and receipts[0]['profile'] == 'shell'
    client.run(['git', '-C', str(tmp_path), 'status'], text=True)
    assert receipts[-1]['profile'] == 'git' and receipts[-1]['args'] == ['status']


@pytest.mark.asyncio
async def test_codex_dispatches_completion_and_version(monkeypatch):
    from core.backends.codex_code import CodexCodeClient
    calls = []
    async def execute(profile, **kwargs):
        calls.append((profile, kwargs))
        return {'stdout': b'cli-output', 'stderr': b'', 'returncode': 0}
    monkeypatch.setattr(client, 'execute_async', execute)
    codex = CodexCodeClient()
    assert await codex.health_check()
    assert (await codex._spawn(['/custom/codex', 'exec', '--json'], '/scratch', 'prompt',
                             {'JWT_SECRET': 'canary'}))[0] == b'cli-output'
    assert calls[0][1]['args'] == ['--version']
    assert calls[1][1]['input'] == 'prompt' and 'canary' not in str(calls)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX file ownership/link/dirfd checks')
def test_shared_auth_refuses_symlink_hardlink_fifo_owner_and_parent(tmp_path):
    from core.worker.auth_file import read
    home = tmp_path / 'home'
    home.mkdir()
    path = home / 'auth.json'
    path.write_text('{"tokens":{}}')
    uid = os.geteuid()
    assert read(path, uid) == {'tokens': {}}
    with pytest.raises(ValueError):
        read(path, uid + 1)
    os.link(path, home / 'hardlink')
    with pytest.raises(ValueError):
        read(path, uid)
    (home / 'hardlink').unlink()
    path.rename(home / 'real')
    path.symlink_to(home / 'real')
    with pytest.raises(OSError):
        read(path, uid)
    path.unlink()
    os.mkfifo(path)
    with pytest.raises(ValueError):
        read(path, uid)
    alias = tmp_path / 'alias'
    alias.symlink_to(home, target_is_directory=True)
    with pytest.raises(OSError):
        read(alias / 'auth.json', uid)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX atomic renameat and worker ownership')
def test_shared_auth_atomic_compare_replace(tmp_path):
    from core.worker.auth_file import read, replace
    path = tmp_path / 'auth.json'
    path.write_text('{"tokens":{"refresh_token":"old"}}')
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    uid = os.geteuid()
    assert not replace(path, uid, 'wrong', {'tokens': {}})
    assert replace(path, uid, expected, {'tokens': {'refresh_token': 'new'}})
    assert path.stat().st_uid == uid and path.stat().st_nlink == 1
    assert stat.S_IMODE(path.stat().st_mode) == 0o660
    assert read(path, uid)['tokens']['refresh_token'] == 'new'
    assert not list(tmp_path.glob('.auth-bob-*'))


@pytest.mark.skipif(not hasattr(os, 'O_PATH'), reason='Linux O_PATH and directory permissions')
def test_shared_auth_traverses_0711_ancestor_and_refuses_ancestor_symlink(tmp_path, monkeypatch):
    from core.worker.auth_file import directory, read
    ancestor = tmp_path / 'search-only'
    home = ancestor / 'lane/codex-home'
    home.mkdir(parents=True)
    path = home / 'auth.json'
    path.write_text('{"tokens":{"access_token":"synthetic"}}')
    ancestor.chmod(0o711)
    real_open, opened = os.open, []
    def tracked_open(name, flags, *args, **kwargs):
        opened.append((name, flags))
        return real_open(name, flags, *args, **kwargs)
    monkeypatch.setattr(os, 'open', tracked_open)
    assert read(path, os.geteuid())['tokens']['access_token'] == 'synthetic'
    # Check flags as well: root/the owner could otherwise mask the EACCES bug
    # even though a service user only receives the 0711 ancestor's search bit.
    assert all(flags & os.O_PATH for name, flags in opened[:-2])
    assert all(flags & os.O_NOFOLLOW for name, flags in opened[1:])
    assert not opened[-2][1] & os.O_PATH  # final directory is readable/fsyncable
    with directory(home) as fd:
        assert 'auth.json' in os.listdir(fd)
        os.fsync(fd)
    alias = tmp_path / 'alias'
    alias.symlink_to(ancestor, target_is_directory=True)
    with pytest.raises(OSError):
        read(alias / 'lane/codex-home/auth.json', os.geteuid())
    # Also exercise actual search-only traversal when running without root.
    try:
        ancestor.chmod(0o111)
        assert read(path, os.geteuid())['tokens']['access_token'] == 'synthetic'
    finally:
        ancestor.chmod(0o711)


@pytest.mark.skipif(sys.platform != 'linux' or platform.machine().lower() not in {'x86_64', 'amd64'},
                    reason='real Linux x86_64 seccomp')
@pytest.mark.parametrize('network', [False, True])
def test_seccomp_allows_anonymous_pairs_but_denies_host_unix_sockets(network):
    # Install only in a child: never constrain the pytest process. An actual
    # syscall test catches BPF jumps/type filtering, not just source spelling.
    source = '''import errno, socket, sys
sys.path.insert(0, sys.argv[1])
from core.worker.restrict import install
install(network=sys.argv[2] == 'True')
for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_SEQPACKET):
 a, b = socket.socketpair(socket.AF_UNIX, kind)
 with a, b:
  a.send(b'pair-ok')
  assert b.recv(64) == b'pair-ok'
for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM, socket.SOCK_SEQPACKET):
 try:
  s = socket.socket(socket.AF_UNIX, kind)
 except OSError as exc:
  assert exc.errno == errno.EPERM, exc
 else:
  s.close()
  raise AssertionError('host/abstract Unix socket creation allowed')'''
    result = subprocess.run([sys.executable, '-I', '-c', source,
                             str(Path(__file__).resolve().parents[1]), str(network)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def execution_sites(source, *, include_legacy=True):
    """Import/alias-aware inventory; includes indirect process launch and code loading."""
    tree = ast.parse(source)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split('.')[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                aliases[alias.asname or alias.name] = (node.module or '') + '.' + alias.name
    def symbol(node):
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return symbol(node.value) + '.' + node.attr
        return ''
    # Track simple callable aliases too; taking a dangerous callable is itself a site.
    dangerous = {'eval', 'exec', 'compile', '__import__', 'builtins.eval', 'builtins.exec',
        'builtins.compile', 'builtins.__import__', 'importlib.import_module',
        'importlib.util.spec_from_file_location', 'importlib.util.module_from_spec',
        'runpy.run_path', 'runpy.run_module', 'pickle.loads', 'marshal.loads',
        'langchain_mcp_adapters.client.MultiServerMCPClient', 'mcp.client.stdio.stdio_client',
        'anyio.open_process', 'anyio.run_process', 'trio.run_process', 'pty.spawn',
        'concurrent.futures.ProcessPoolExecutor', 'os.fork', 'os.forkpty',
        'os.posix_spawn', 'os.posix_spawnp', 'types.FunctionType', 'ctypes.CDLL',
        'ctypes.PyDLL', 'ctypes.WinDLL', 'ctypes.OleDLL', 'ctypes.cdll.LoadLibrary', 'ctypes.pydll.LoadLibrary'}
    def is_dangerous(name):
        return (name in dangerous or name.startswith(('subprocess.', 'asyncio.create_subprocess',
                'os.exec', 'os.spawn', 'posix.exec', 'posix.spawn')) and
                not name.endswith(('CompletedProcess', 'TimeoutExpired', 'SubprocessError', 'PIPE', 'DEVNULL', 'STDOUT'))
                or name in {'os.system', 'os.popen', 'posix.system', 'posix.popen'}
                or name.startswith(('multiprocessing.', 'joblib.', 'pexpect.'))
                or name.endswith(('.subprocess_exec', '.subprocess_shell'))
                or name.endswith(('.exec_module', '.load_module')))
    def legacy_test(node):
        return (isinstance(node, ast.Compare) and len(node.ops) == 1 and
                isinstance(node.ops[0], ast.Eq) and len(node.comparators) == 1 and
                isinstance(node.left, ast.Call) and symbol(node.left.func) == 'core.worker.mode' and
                not node.left.args and not node.left.keywords and
                isinstance(node.comparators[0], ast.Constant) and node.comparators[0].value == 'legacy')

    def walk(node, guarded=False):
        yield node, guarded
        if isinstance(node, (ast.If, ast.IfExp)) and legacy_test(node.test):
            yield from walk(node.test, guarded)
            body = node.body if isinstance(node.body, list) else [node.body]
            other = node.orelse if isinstance(node.orelse, list) else [node.orelse]
            for child in body:
                yield from walk(child, True)
            for child in other:
                yield from walk(child, guarded)
        else:
            for child in ast.iter_child_nodes(node):
                yield from walk(child, guarded)

    sites = []
    for node, guarded in walk(tree):
        if guarded and not include_legacy:
            continue
        if isinstance(node, ast.Call) and is_dangerous(symbol(node.func)):
            sites.append(symbol(node.func))
        elif (isinstance(node, ast.Call) and symbol(node.func) == 'getattr' and len(node.args) >= 2
              and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
              and is_dangerous(symbol(node.args[0]) + '.' + node.args[1].value)):
            sites.append(symbol(node.args[0]) + '.' + node.args[1].value)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and is_dangerous(symbol(node.value)):
            sites.append(symbol(node.value))
    return Counter(sites)


@pytest.mark.parametrize('code', ['import subprocess as s\ns.run([])', 'from subprocess import Popen as p\np([])',
    'import os as o\no.system("id")', 'exec("bad")', 'import importlib\nimportlib.import_module("job")',
    'from subprocess import run\nf = run\nf([])',
    'from langchain_mcp_adapters.client import MultiServerMCPClient as M\nM({})',
    'import os\nos.posix_spawn("x",[],{})', 'import multiprocessing as m\nm.Process(target=f)',
    'import subprocess as s\ngetattr(s,"run")([])', 'import ctypes as c\nc.CDLL(module_path)'])
def test_inventory_detects_new_execution(code):
    assert execution_sites(code)


@pytest.mark.parametrize('code', [
    'from core.worker import mode\nimport subprocess\nif mode() == "legacy":\n subprocess.run([])',
    'from core.worker import mode as execution_mode\nimport subprocess as s\nif execution_mode() == "legacy":\n s.run([])',
    'from core.worker import mode\nfrom subprocess import run\nif mode() == "legacy":\n f = run\n f([])',
])
def test_inventory_accepts_only_the_explicit_legacy_branch(code):
    assert execution_sites(code)
    assert not execution_sites(code, include_legacy=False)


@pytest.mark.parametrize('guard', ['mode() == "worker"', 'True', 'os.getenv("MODE") == "legacy"'])
def test_inventory_rejects_other_guards(guard):
    code = 'from core.worker import mode\nimport subprocess, os\nif ' + guard + ':\n subprocess.run([])'
    assert execution_sites(code, include_legacy=False)


def test_inventory_rejects_spawn_after_or_in_else_of_legacy_guard():
    prefix = 'from core.worker import mode\nimport subprocess\nif mode() == "legacy":\n pass\n'
    assert execution_sites(prefix + 'subprocess.run([])', include_legacy=False)
    assert execution_sites(prefix + 'else:\n subprocess.run([])', include_legacy=False)


def test_core_execution_inventory():
    core = Path(__file__).resolve().parents[1]
    found = {}
    for folder in ('core', 'api', 'tools'):
        for path in (core / folder).rglob('*.py'):
            sites = execution_sites(path.read_text(encoding='utf-8-sig'), include_legacy=False)
            if sites:
                found[path.relative_to(core).as_posix()] = sites
    assert found == {
        'core/build/contracts.py': Counter({'compile': 1}),  # parse-only, never exec
        'core/tool_jobs/checked_runner.py': Counter({'subprocess.Popen': 1}),  # installed worker child only
        'core/worker/daemon.py': Counter({'subprocess.Popen': 1, 'ctypes.CDLL': 1}),
        'core/worker/journal.py': Counter({'ctypes.CDLL': 1}),
        'core/worker/mcp_runner.py': Counter({'anyio.open_process': 1}),
        'core/worker/restrict.py': Counter({'ctypes.CDLL': 1}),
        'core/worker/landlock.py': Counter({'ctypes.CDLL': 1}),
    }


def test_http_shared_refresh_publishes_via_worker(monkeypatch, tmp_path):
    from contextlib import contextmanager
    from core.backends import openai_codex_auth as oauth
    from core.worker import auth_file
    from core.lane_config import LaneConfiguration
    store = {'tokens': {'access_token': 'old-access', 'refresh_token': 'old-refresh'}}
    lane = LaneConfiguration(tmp_path, {'BOB_WORKER_ID': 'a', 'BOB_WORKER_UID': '123'}, {}, None)
    monkeypatch.setattr(oauth, 'read_codex_auth', lambda lane: json.loads(json.dumps(store)))
    @contextmanager
    def directory(path):
        yield 42
    monkeypatch.setattr(auth_file, 'directory', directory)
    monkeypatch.setattr(auth_file, 'read_at', lambda fd, uid: json.dumps(store).encode())
    monkeypatch.setattr(client, 'request', lambda profile, **kwargs: {'profile': profile, **kwargs})
    def exchange(request):
        assert request['profile'] == 'auth-replace'
        value = json.loads(request['input'])
        assert value['expected'] == hashlib.sha256(json.dumps(store).encode()).hexdigest()
        store.clear()
        store.update(value['auth'])
        return {'returncode': 0}
    monkeypatch.setattr(client, 'exchange', exchange)
    auth = oauth.LaneCodexAuth(lane)
    saved = auth._save({'access_token': 'new-access', 'refresh_token': 'new-refresh'}, auth.load())
    assert saved.refresh == 'new-refresh'
    assert not list(tmp_path.rglob('.auth-bob-*'))


@pytest.mark.asyncio
async def test_async_cancellation_cancels_queue_observer(monkeypatch, payload):
    import concurrent.futures
    monkeypatch.setattr(client, 'request', lambda *args, **kwargs: payload)
    monkeypatch.setattr(client, 'unavailable_reason', lambda *args: None)
    future = concurrent.futures.Future()
    calls = []
    def submit(context, request):
        calls.append((context, request))
        return future
    monkeypatch.setattr(client, 'queue_bridge', lambda: SimpleNamespace(submit=submit))
    task = asyncio.create_task(client.execute_async('shell'))
    for _ in range(200):
        if calls:
            break
        await asyncio.sleep(.01)
    assert calls
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(.01)
    assert future.cancelled()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX no-follow service workspace writers')
def test_workspace_data_writer_does_not_follow_worker_links(tmp_path):
    from core.worker import workspace
    secret = tmp_path / 'service-secret'
    secret.write_text('canary')
    root = tmp_path / 'workspace'
    root.mkdir()
    target = root / 'generated.py'
    target.symlink_to(secret)
    workspace.write(target, 'generated data')
    assert secret.read_text() == 'canary' and not target.is_symlink()
    (root / 'escape').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(OSError):
        workspace.write(root / 'escape/service-secret', 'must not write')
    with pytest.raises(OSError):
        workspace.mkdir(root / 'escape/newdir')
    assert secret.read_text() == 'canary'


def test_codex_never_reads_project_briefing_in_core(tmp_path):
    from core.backends.codex_code import CodexCodeClient
    (tmp_path / 'CLAUDE.md').write_text('SERVICE_CANARY')
    cli = CodexCodeClient(cwd=str(tmp_path))
    assert cli._brief_prompt('task', {'brief': True, 'read_repo': True}) == 'task'


@pytest.mark.asyncio
async def test_mcp_discovery_calls_and_browser_sequence_use_worker(monkeypatch):
    from core.mcp import client as mcp
    calls = []
    async def execute(profile, **kwargs):
        assert profile == 'mcp'
        request = json.loads(kwargs['input'])
        calls.append(request)
        if request['operation'] == 'list':
            value = [{'name': 'read_text_file' if request['server'] == 'filesystem' else 'browser_navigate',
                      'description': 'probe', 'inputSchema': {'type': 'object',
                          'properties': {'path': {'type': 'string'}}, 'required': ['path']}}]
        else:
            value = {'content': [{'type': 'text', 'text': 'worker result'}]}
        return {'returncode': 0, 'stdout': json.dumps(value).encode()}
    monkeypatch.setattr(mcp, 'execute_async', execute)
    mcp._reset_mcp_cache()
    try:
        tools = {tool.name: tool for tool in await mcp.get_mcp_tools()}
        await tools['mcp__filesystem__read_text_file'].ainvoke({'path': 'test.txt'})
        await tools['mcp__browser__sequence'].ainvoke({'calls': [{'name': 'browser_navigate', 'arguments': {'url': 'about:blank'}}]})
        assert calls[-2]['operation'] == 'call' and calls[-1]['operation'] == 'sequence'
    finally:
        mcp._reset_mcp_cache()


@pytest.mark.asyncio
async def test_worker_mcp_stdio_transport_roundtrip(monkeypatch):
    import anyio
    from mcp import types
    from mcp.shared.message import SessionMessage
    from core.worker import mcp_runner
    output, stdout = anyio.create_memory_object_stream(10)
    await output.send(b'{"jsonrpc":"2.0","method":"probe"}\n')
    sent = []
    written = asyncio.Event()
    class Input:
        async def send(self, value):
            sent.append(value)
            written.set()
        async def aclose(self):
            process.returncode = 0
    class Process:
        returncode = None
        stdin = Input()
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def wait(self): return 0
        def kill(self): self.returncode = -9
    process = Process()
    process.stdout = stdout
    async def open_process(command, **kwargs):
        assert command == ['/usr/bin/node', '/installed/server.js'] and not kwargs['start_new_session']
        return process
    monkeypatch.setattr(mcp_runner.anyio, 'open_process', open_process)
    async with mcp_runner.transport(['/installed/server.js']) as (read, write):
        received = await asyncio.wait_for(read.receive(), timeout=2)
        assert received.message.root.method == 'probe'
        await write.send(SessionMessage(types.JSONRPCMessage.model_validate({'jsonrpc': '2.0', 'method': 'outgoing'})))
        await asyncio.wait_for(written.wait(), timeout=2)
        assert json.loads(sent[0])['method'] == 'outgoing'
    await output.aclose()
