"""Bounded Builder network contracts; fake loopback upstreams, never Internet."""
import asyncio
import base64
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlparse
import uuid

import pytest

from core.builder.egress_proxy import Proxy, public_connect, read_json, send_json
from core.builder.network import DEFAULTS, domain, network_decide, network_key, public_address
from core.builder.service import Builder, execution
from core.execution.models import Authority, ExecutionError
from core.execution.store import Store


@pytest.fixture(autouse=True)
def mock_redis():
    # Override the broad suite fixture: these tests have no model transport.
    yield


@pytest.mark.parametrize('mode', ['suggest', 'approve', 'auto', 'yolo'])
@pytest.mark.parametrize('host,method,extra,expected', [
    ('pypi.org', 'GET', (), 'allow'),
    ('pypi.org', 'HEAD', (), 'allow'),
    ('pypi.org', 'OPTIONS', (), 'allow'),
    ('pypi.org', 'CONNECT', (), 'allow'),
    ('registry.npmjs.org', 'POST', (), 'ask'),
    ('github.com', 'PUT', (), 'ask'),
    ('example.com', 'GET', (), 'ask'),
    ('example.com', 'CONNECT', (), 'ask'),
    ('example.com', 'DELETE', (), 'ask'),
    ('example.com', 'TRACE', (), 'ask'),
    ('example.com', 'GET', ('example.com',), 'allow'),
    ('example.com', 'POST', ('example.com',), 'ask'),
])
def test_policy_matrix(mode, host, method, extra, expected):
    assert network_decide(mode, host, method, extra) == ('allow' if mode == 'yolo' else expected)


def test_exact_domains_methods_and_fail_closed_mode():
    assert network_decide('auto', 'PYPI.ORG.', 'GET') == 'allow'
    assert network_decide('auto', 'pypi.org.evil.test', 'GET') == 'ask'
    assert network_decide('auto', 'sub.pypi.org', 'GET') == 'ask'
    assert network_decide('unknown', 'pypi.org', 'GET', session=True) == 'deny'
    assert network_decide('approve', 'example.com', 'POST', session=True) == 'allow'
    assert network_key('example.com', 'GET') != network_key('example.com', 'POST')
    assert not DEFAULTS['upload_hosts']  # Installers download; publishing/audit needs approval.
    for value in ('*.example.com', 'http://example.com', 'bad\r\nhost', '-bad.test', 'a..b'):
        with pytest.raises(ValueError):
            domain(value)


@pytest.mark.parametrize('address', [
    '127.0.0.1', '10.0.0.1', '100.65.0.1', '169.254.169.254', '192.168.2.1',
    '0.0.0.0', '192.0.2.1', '::1', 'fc00::1', 'fe80::1', '::ffff:8.8.8.8', '2002:0808:0808::1',
    '224.0.0.1', 'ff0e::1', '64:ff9b::7f00:1', '64:ff9b:1::7f00:1',
])
def test_internal_reserved_and_transition_addresses_refused(address):
    assert not public_address(address)


async def test_dns_rebinding_guard_rejects_mixed_answers_before_connect(monkeypatch):
    import socket
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
    ]))
    connect = AsyncMock()
    monkeypatch.setattr(asyncio, 'open_connection', connect)
    with pytest.raises(ValueError, match='private or reserved'):
        await public_connect('example.com', 443)
    connect.assert_not_called()


async def test_connector_pins_validated_numeric_ip(monkeypatch):
    import socket
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, 'getaddrinfo', AsyncMock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
    ]))
    connect = AsyncMock(return_value=('reader', 'writer'))
    monkeypatch.setattr(asyncio, 'open_connection', connect)
    assert await public_connect('example.com', 443) == ('reader', 'writer')
    connect.assert_awaited_once_with('8.8.8.8', 443, family=socket.AF_INET)


class FakeLease:
    def __init__(self, decision):
        self.expiry = time.time() + 60
        self.closed = self.revoking = False
        self.tasks, self.requests = set(), 0
        self.job_id = 'job-one'
        self.profile = 'builder'
        self.decision = decision
        self.events = []

    async def event(self, value):
        self.events.append(value)
        if value['type'] == 'log':
            return {'saved': True}
        return {'decision': self.decision, **({'approval_id': 'approval-one'} if self.decision != 'allow' else {})}


@asynccontextmanager
async def proxy_fixture(decision='allow', *, tunnel=False):
    upstream_requests, connects = [], []
    handlers = set()
    async def upstream(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            if tunnel:
                data = await reader.readexactly(4)
                upstream_requests.append(data)
                writer.write(b'pong')
            else:
                data = await reader.readuntil(b'\r\n\r\n')
                length = next((int(line.split(b':', 1)[1]) for line in data.lower().split(b'\r\n')
                               if line.startswith(b'content-length:')), 0)
                upstream_requests.append(data + await reader.readexactly(length))
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok')
            await writer.drain()
            if tunnel:
                await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())
    remote = await asyncio.start_server(upstream, '127.0.0.1', 0)
    async def connector(host, port):
        connects.append((host, port))
        return await asyncio.open_connection('127.0.0.1', remote.sockets[0].getsockname()[1])
    proxy = Proxy(-1, connector=connector)
    lease = FakeLease(decision)
    token = 'a' * 64
    proxy.leases[token] = lease
    server = await asyncio.start_server(proxy.request, '127.0.0.1', 0)
    async def exchange(target='http://pypi.org/simple/pkg', method='GET', *, auth=True, headers='', body=b''):
        reader, writer = await asyncio.open_connection('127.0.0.1', server.sockets[0].getsockname()[1])
        credential = base64.b64encode(('bob:' + token).encode()).decode()
        raw = f'{method} {target} HTTP/1.1\r\nHost: ignored.test\r\n'
        if auth:
            raw += 'Proxy-Authorization: Basic ' + credential + '\r\n'
        writer.write((raw + headers + '\r\n').encode() + body)
        await writer.drain()
        return reader, writer
    try:
        yield SimpleNamespace(proxy=proxy, lease=lease, exchange=exchange, connects=connects,
                              upstream=upstream_requests, port=server.sockets[0].getsockname()[1])
    finally:
        for task in list(lease.tasks):
            task.cancel()
        await asyncio.gather(*list(lease.tasks), return_exceptions=True)
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*list(handlers), return_exceptions=True)
        server.close()
        remote.close()
        await server.wait_closed()
        await remote.wait_closed()


async def finish(reader, writer):
    try:
        return await asyncio.wait_for(reader.read(), 3)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.mark.parametrize('decision,status', [('allow', b'200'), ('deny', b'403'), ('ask', b'403')])
async def test_http_allow_deny_ask_and_per_job_logging(decision, status):
    async with proxy_fixture(decision) as h:
        response = await finish(*(await h.exchange()))
        assert b'HTTP/1.1 ' + status in response
        assert bool(h.connects) == (decision == 'allow')
        event = h.lease.events[-1]
        assert event['type'] == 'log' and event['decision'] == decision
        assert event['domain'] == 'pypi.org' and event['method'] == 'GET'
        assert event['job_id'] == 'job-one'
        if decision == 'ask':
            assert b'bob-proxy: domain pypi.org needs approval (approval_id approval-one)' in response
        if decision == 'allow':
            assert h.upstream[0].startswith(b'GET /simple/pkg HTTP/1.1')
            assert b'proxy-authorization' not in h.upstream[0].lower()
            assert event['bytes_in'] > 0 and event['bytes_out'] > 0
        else:
            assert event['approval_id'] == 'approval-one'
            assert event['bytes_in'] == event['bytes_out'] == 0


@pytest.mark.parametrize('invalid', ['missing', 'expired', 'revoking', 'closed'])
async def test_builder_job_token_required_and_no_direct_fallback(invalid):
    async with proxy_fixture() as h:
        if invalid == 'expired':
            h.lease.expiry = 1
        elif invalid in {'revoking', 'closed'}:
            setattr(h.lease, invalid, True)
        response = await finish(*(await h.exchange(auth=invalid != 'missing')))
        assert b'407' in response and b'job token required' in response
        assert not h.connects and not h.upstream and not h.lease.events


async def test_plain_http_upload_body_and_hop_header_stripping():
    async with proxy_fixture() as h:
        response = await finish(*(await h.exchange(method='POST', body=b'abc',
            headers='Content-Length: 3\r\nConnection: x-private\r\nX-Private: remove\r\n')))
        assert b'200 OK' in response
        assert h.upstream[0].endswith(b'\r\n\r\nabc')
        assert b'x-private' not in h.upstream[0].lower()


async def test_explicitly_allowed_extension_method_is_forwarded():
    async with proxy_fixture() as h:
        assert b'200 OK' in await finish(*(await h.exchange(method='PROPFIND')))
        assert h.upstream[0].startswith(b'PROPFIND /simple/pkg HTTP/1.1')
        assert h.lease.events[-1]['method'] == 'PROPFIND'


@pytest.mark.parametrize('target,headers', [
    ('http://pypi.org:8080/', ''), ('http://user:pass@pypi.org/', ''),
    ('http://pypi.org/', 'Transfer-Encoding: chunked\r\n'),
    ('http://pypi.org/', 'Content-Length: 0\r\nContent-Length: 3\r\n'),
    ('http://pypi.org/', 'Upgrade: websocket\r\n'),
])
async def test_ambiguous_requests_and_nonstandard_ports_never_connect(target, headers):
    async with proxy_fixture() as h:
        assert b'502' in await finish(*(await h.exchange(target, headers=headers)))
        assert not h.connects


async def test_connect_tunnel_counts_bytes_and_only_exposes_host():
    async with proxy_fixture(tunnel=True) as h:
        reader, writer = await h.exchange('pypi.org:443', 'CONNECT')
        assert b'200 Connection Established' in await reader.readuntil(b'\r\n\r\n')
        writer.write(b'ping')
        await writer.drain()
        assert await reader.readexactly(4) == b'pong'
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if h.lease.events[-1]['type'] == 'log':
                break
            await asyncio.sleep(.01)
        event = h.lease.events[-1]
        assert event['type'] == 'log'
        assert (event['method'], event['bytes_in'], event['bytes_out']) == ('CONNECT', 4, 4)
        assert h.connects == [('pypi.org', 443)]


@pytest.mark.parametrize('exchanged,decision', [(True, 'allow'), (False, 'error')])
async def test_connect_tunnel_client_reset_after_traffic_is_a_normal_close(exchanged, decision):
    import socket, struct
    async with proxy_fixture(tunnel=True) as h:
        reader, writer = await h.exchange('pypi.org:443', 'CONNECT')
        assert b'200 Connection Established' in await reader.readuntil(b'\r\n\r\n')
        if exchanged:
            writer.write(b'ping')
            await writer.drain()
            assert await reader.readexactly(4) == b'pong'
        # SO_LINGER 0 makes close() send RST, like a TLS client leaving unread data.
        writer.get_extra_info('socket').setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        writer.transport.abort()
        for _ in range(200):
            if h.lease.events and h.lease.events[-1]['type'] == 'log':
                break
            await asyncio.sleep(.01)
        event = h.lease.events[-1]
        assert event['type'] == 'log' and event['method'] == 'CONNECT'
        assert event['decision'] == decision


async def test_shell_dispatch_receipt_and_token_redaction(monkeypatch):
    from core.builder import network_client
    import core.worker.client as client
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    instances = []
    class Lease:
        def __init__(self, *args):
            self.token = 'a' * 64
            self.url = 'http://bob:' + self.token + '@127.0.0.1:21004'
            self.events, self.approvals, self.job_id = [{'decision': 'allow'}], [], args[4]
            self.closed = False
            instances.append(self)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.closed = True
    monkeypatch.setattr(network_client, 'JobNetwork', Lease)
    monkeypatch.setattr(client, 'configuration', lambda: {})
    dispatch = AsyncMock(return_value={'returncode': 0, 'stdout': ('a' * 64).encode(), 'stderr': b'',
                                      'cleanup': 'idle_proved', 'execution_id': 'execution-one'})
    monkeypatch.setattr(client, 'execute_async', dispatch)
    context = execution.set({'row': {}, 'approval_id': 'job-one'})
    try:
        result = await Builder(None).worker(auth, Path.cwd(), 'shell_run', {'command': 'npm install'})
    finally:
        execution.reset(context)
    payload = json.loads(dispatch.call_args.kwargs['input'])
    assert payload['network'] is True and payload['proxy_url'] == instances[0].url
    assert result['stdout'] == '[job-token]' and result['network_log'] == instances[0].events
    assert result['job_id'] == 'job-one' and instances[0].closed
    with pytest.raises(ExecutionError, match='builder_context_unavailable'):
        await Builder(None).worker(auth, Path.cwd(), 'shell_run', {'command': 'ls'})
    dispatch.return_value['stdout'] = b'{"adapter":"builder-command/1","result":{"bytes":1}}'
    await Builder(None).worker(auth, Path.cwd(), 'fs_write', {'path': 'a', 'content': 'x'})
    assert json.loads(dispatch.call_args.kwargs['input'])['network'] is False
    assert len(instances) == 1


@pytest.mark.parametrize('failure', ['configuration', 'enrollment', 'submit', 'cleanup', 'drain'])
async def test_proxy_setup_refused_but_submitted_command_failure_stays_unknown(monkeypatch, failure):
    from core.builder import network_client
    from core.worker import client
    from core.worker.contract import WorkerUnavailable
    row = {'project_id': uuid.uuid4(),'session_id':'session'}
    approval = {'id': 'approval-one', 'conversation_id': uuid.uuid4(), 'project_id': row['project_id'],'session_id':'session',
                'expires_at': time.time()+60, 'execution_state': 'pending', 'turn_id': 'turn-one',
                'payload': {'operation': 'shell_run', 'args': {'command': 'curl example.com'}}}
    record = {**approval, 'result': None}
    @asynccontextmanager
    async def scope(value):
        yield value
    async def lookup(sql,*args):
        return None if 'FROM workflow_workspace_leases' in sql else row
    connection = SimpleNamespace(execute=AsyncMock(), fetchrow=AsyncMock(side_effect=lookup), transaction=lambda: scope(None))
    async def save(sql, identifier, *args):
        assert identifier == approval['id']
        record['execution_state'] = ('unknown' if "execution_state='unknown'" in sql else 'failed' if "execution_state='failed'" in sql else args[1] if len(args)>1 else 'completed') if args else 'unknown'
        if args:
            record['result'] = json.loads(args[0])
    pool = SimpleNamespace(acquire=lambda: scope(connection),
                           fetchval=AsyncMock(side_effect=[approval['id'], None]), execute=AsyncMock(side_effect=save))
    builder = Builder(pool)
    builder.store = SimpleNamespace(mode=AsyncMock(return_value=row), checkpoint=AsyncMock(),
                                    approval=AsyncMock(return_value=record))
    builder.workspace = lambda *args: Path.cwd()
    def config():
        if failure == 'configuration':
            raise WorkerUnavailable('synthetic unavailable configuration')
        return {}
    class Lease:
        token, url, job_id = 'synthetic-token', 'http://proxy.invalid', approval['id']
        events, approvals = [], []
        def __init__(self, *args): pass
        async def __aenter__(self):
            if failure == 'enrollment':
                raise OSError('synthetic enrollment failure')
            return self
        async def __aexit__(self, *args):
            if failure == 'drain':
                raise ValueError('synthetic unproved drain')
    monkeypatch.setattr(client, 'configuration', config)
    monkeypatch.setattr(network_client, 'JobNetwork', Lease)
    dispatch = AsyncMock(return_value={'returncode': 0, 'stdout': b'', 'stderr': b'',
                                      'cleanup': 'unproved' if failure == 'cleanup' else 'idle_proved'})
    if failure == 'submit':
        dispatch.side_effect = ExecutionError('synthetic_submission_failure', 503)
    monkeypatch.setattr(client, 'execute_async', dispatch)
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    result = await builder.execute(auth, approval)
    if failure in {'configuration', 'enrollment'}:
        assert result == {'approval_id': approval['id'], 'status': 'refused',
                          'error': {'code': 'builder_proxy_unavailable'}}
        assert record['execution_state'] == 'failed' and record['result'] == result
        dispatch.assert_not_awaited()
        assert await builder.execute(auth, approval) == result
    else:
        assert result['error']['code'] == 'builder_outcome_unknown' and result['end_turn']
        assert record['execution_state'] == 'unknown'
        assert (await builder.execute(auth, approval))['status'] == 'unknown'
        dispatch.assert_awaited_once()
    builder.store.checkpoint.assert_awaited_once()


async def test_network_cards_emitted_from_server_policy_and_logs_saved(monkeypatch):
    from core.builder.network_client import JobNetwork
    from core.tools.ask import _writer
    from core.builder.egress_proxy import Lease
    frames, records = [], []
    card = {'schema_version': 1, 'approval_id': 'approval-one', 'kind': 'network'}
    policy = AsyncMock(return_value={'decision': 'ask', 'approval': card, 'approval_id': 'approval-one'})
    log = AsyncMock(side_effect=lambda *args: records.append(args[-1]))
    job = JobNetwork(SimpleNamespace(network=SimpleNamespace(policy=policy, log=log)),
                     None, {'conversation_id': 'conversation'}, 'curl example.com', 'job', {}, 120)
    job.reader = asyncio.StreamReader()
    for event in [{'type': 'policy', 'domain': 'example.com', 'method': 'GET'},
                  {'type': 'policy', 'domain': 'example.com', 'method': 'GET'},
                  {'type': 'log', 'domain': 'example.com', 'method': 'GET', 'decision': 'ask'}]:
        job.reader.feed_data(json.dumps(event).encode() + b'\n')
    job.reader.feed_eof()
    class Writer:
        def write(self, value): pass
        async def drain(self): pass
    job.writer = Writer()
    token = _writer.set(frames.append)
    try:
        await job.serve()
    finally:
        _writer.reset(token)
    assert job.approvals == [card] and len(frames) == 1
    assert '```bob-builder-approval' in frames[0]['content']
    assert records[0]['decision'] == 'ask' and job.events[0]['domain'] == 'example.com'


def test_proxy_environment_is_set_only_by_builder_shell_runner(monkeypatch, tmp_path_factory):
    from core.worker import builder_runner as runner
    if os.name == 'nt':
        root = Path(__file__).parents[1] / 'test-db' / ('network-env-' + uuid.uuid4().hex)
        root.mkdir(mode=0o777, parents=True)
    else:
        root = tmp_path_factory.mktemp('network-env')
    captured = []
    monkeypatch.setattr(runner, 'os', SimpleNamespace(name='posix',
        environ={'PATH': '/bin', 'HTTPS_PROXY': 'untrusted', 'NO_PROXY': '*'},
        chdir=lambda path: None, fchdir=lambda fd: None,
        execve=lambda program, argv, env: captured.append((program, argv, env))))
    proxy = 'http://bob:' + 'a' * 64 + '@127.0.0.1:21004'
    payload = {'operation': 'shell_run', 'args': {'command': 'true'}, 'network': True, 'proxy_url': proxy}
    # Real execve never returns. Stop after capturing its arguments in this fixture.
    class Executed(Exception): pass
    def execve(program, argv, env):
        captured.append((program, argv, env))
        raise Executed()
    runner.os.execve = execve
    with pytest.raises(Executed):
        runner.execute(payload, root)
    env = captured[-1][2]
    assert all(env[k] == proxy for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'))
    assert env['NO_PROXY'] == env['no_proxy'] == '' and env['npm_config_audit'] == 'false'
    with pytest.raises(Executed):
        runner.execute({**payload, 'network': False}, root)
    assert not any(k.lower().endswith('_proxy') for k in captured[-1][2])
    assert captured[-1][0] == '/bin/sh'


@pytest.mark.parametrize('profile,seconds', [('builder', 60), ('codex', 1800), ('mcp', 120)])
async def test_control_requires_core_peer_uid_and_revokes_active_tasks(monkeypatch, profile, seconds):
    import socket
    import struct
    monkeypatch.setattr(socket, 'SO_PEERCRED', getattr(socket, 'SO_PEERCRED', 17), raising=False)
    class Writer:
        def __init__(self, uid): self.uid, self.values, self.closed = uid, [], False
        def get_extra_info(self, name): return self
        def getsockopt(self, *args): return struct.pack('3i', 1, self.uid, self.uid)
        def write(self, raw): self.values.append(json.loads(raw))
        async def drain(self): pass
        def close(self): self.closed = True
        async def wait_closed(self): pass
    def reader(value):
        stream = asyncio.StreamReader()
        stream.feed_data(json.dumps(value).encode() + b'\n')
        return stream
    proxy = Proxy(123, max_timeout=3600)
    token = 'b' * 64
    value = {'token': token, 'expires_at': time.time() + seconds, 'job_id': 'job', 'profile': profile}
    denied = Writer(124)  # worker/other lane cannot enroll, even knowing the protocol.
    await proxy.control(reader(value), denied)
    assert denied.closed and not proxy.leases and not denied.values
    approved = Writer(123)
    enrollment = asyncio.create_task(proxy.control(reader(value), approved))
    try:
        for _ in range(100):
            if approved.values:
                break
            await asyncio.sleep(.001)
        assert approved.values == [{'ready': True}]
        assert proxy.leases[token].profile == profile
        finished = []
        async def tunnel():
            try:
                await asyncio.Future()
            finally:
                finished.append(True)
        task = asyncio.create_task(tunnel())
        await asyncio.sleep(0)
        proxy.leases[token].tasks.add(task)
        revocation = Writer(123)
        await proxy.control(reader({'type': 'revoke', 'token': token}), revocation)
        assert revocation.values == [{'drained': True}] and task.cancelled() and finished
        await asyncio.wait_for(enrollment, 1)
        assert token not in proxy.leases and approved.closed
    finally:
        enrollment.cancel()
        await asyncio.gather(enrollment, return_exceptions=True)


async def test_cancelled_policy_exchange_cannot_consume_log_acknowledgement():
    from core.builder.egress_proxy import Lease
    values = []
    class Writer:
        def write(self, raw): values.append(json.loads(raw))
        async def drain(self): pass
    reader = asyncio.StreamReader()
    lease = Lease(reader, Writer(), time.time() + 60)
    policy = asyncio.create_task(lease.event({'type': 'policy'}))
    for _ in range(100):
        if values:
            break
        await asyncio.sleep(.001)
    assert values == [{'type': 'policy'}]
    policy.cancel()
    with pytest.raises(asyncio.CancelledError):
        await policy
    log = asyncio.create_task(lease.event({'type': 'log'}))
    await asyncio.sleep(.01)
    assert values == [{'type': 'policy'}]
    reader.feed_data(b'{"decision":"allow"}\n')
    for _ in range(100):
        if len(values) == 2:
            break
        await asyncio.sleep(.001)
    assert values[-1] == {'type': 'log'}
    reader.feed_data(b'{"saved":true}\n')
    assert await asyncio.wait_for(log, 1) == {'saved': True}


def profile_config():
    return {'lane_id': 'lane-one', 'egress_port': 21004, 'egress_uid': 61004,
            'codex_home': '/lane/codex-home', 'profiles': {
                name: {'network': True, 'egress': 'lane-proxy/1'} for name in ('codex', 'mcp')}}


def profile_job(profile='codex'):
    return {'profile': profile, 'execution_id': 'job-one', 'timeout': 300,
            'input': json.dumps({'server': 'browser', 'arguments': {'url': 'https://example.com'},
                                 'remote_servers': {'injected': 'https://example.com'}})}


@pytest.mark.parametrize('profile,host,method,expected', [
    ('codex', 'api.openai.com', 'CONNECT', 'allow'),
    ('codex', 'chatgpt.com', 'CONNECT', 'allow'),
    ('codex', 'AUTH.OPENAI.COM.', 'CONNECT', 'allow'),
    ('codex', 'api.openai.com.evil.test', 'CONNECT', 'deny'),
    ('codex', 'sub.chatgpt.com', 'CONNECT', 'deny'),
    ('codex', 'pypi.org', 'CONNECT', 'deny'),
    ('codex', 'example.com', 'CONNECT', 'deny'),
    ('codex', 'api.openai.com', 'POST', 'deny'),
    ('codex', 'api.openai.com', 'GET', 'deny'),
    ('mcp', 'example.com', 'CONNECT', 'deny'),
    ('mcp', 'chatgpt.com', 'CONNECT', 'deny'),
    ('mcp', 'pypi.org', 'GET', 'deny'),
])
async def test_profile_policy_is_fixed_tls_only_and_never_asks(profile, host, method, expected):
    from core.worker.profile_network import ProfileNetwork
    job = ProfileNetwork(profile_job(profile), profile_config())
    assert await job.policy({'domain': host, 'method': method}) == {'decision': expected}
    assert job.approvals == []
    assert set(DEFAULTS['profiles']['codex']) == {'api.openai.com', 'chatgpt.com', 'auth.openai.com'}
    assert DEFAULTS['profiles']['mcp'] == []
    with pytest.raises(Exception, match='profile proxy boundary missing'):
        ProfileNetwork(profile_job(profile), {**profile_config(), 'profiles': {}})


@pytest.mark.parametrize('profile,target,method,status', [
    ('codex', 'chatgpt.com:443', 'CONNECT', b'200'),
    ('codex', 'auth.openai.com:443', 'CONNECT', b'200'),
    ('codex', 'api.openai.com:443', 'CONNECT', b'200'),
    ('codex', 'pypi.org:443', 'CONNECT', b'403'),
    ('codex', 'http://api.openai.com/', 'POST', b'403'),
    ('mcp', 'chatgpt.com:443', 'CONNECT', b'403'),
    ('mcp', 'example.com:443', 'CONNECT', b'403'),
])
async def test_profile_requests_use_same_proxy_and_per_job_logs(profile, target, method, status):
    from core.worker.profile_network import ProfileNetwork
    job = ProfileNetwork(profile_job(profile), profile_config())
    async with proxy_fixture(tunnel=True) as h:
        h.lease.profile = profile
        async def event(value):
            h.lease.events.append(value)
            if value['type'] == 'policy':
                return await job.policy(value)
            await job.log(value)
            job.events.append({k: v for k, v in value.items() if k != 'type'})
            return {'saved': True}
        h.lease.event = event
        reader, writer = await h.exchange(target, method)
        if status == b'200':
            assert b'200 Connection Established' in await reader.readuntil(b'\r\n\r\n')
            writer.write(b'ping')
            await writer.drain()
            assert await reader.readexactly(4) == b'pong'
            writer.close()
            await writer.wait_closed()
        else:
            response = await finish(reader, writer)
            assert b'403' in response and b'needs approval' not in response
        for _ in range(100):
            if job.events:
                break
            await asyncio.sleep(.001)
        receipt = job.receipt({})
        record = receipt['network_log'][0]
        assert record['job_id'] == 'job-one' and record['profile'] == profile
        assert record['method'] == method and record['decision'] == ('allow' if status == b'200' else 'deny')
        assert bool(h.connects) == (status == b'200')
        assert (record['bytes_in'], record['bytes_out']) == ((4, 4) if status == b'200' else (0, 0))


@pytest.mark.parametrize('profile', ['codex', 'mcp'])
def test_profile_transport_owns_lease_and_redacts_receipt(monkeypatch, profile):
    from core.worker.client import Transport
    from core.worker.profile_network import ProfileNetwork
    lifecycle = []
    async def enter(self):
        lifecycle.append(('enroll', self.job_id))
        return self
    async def exit(self, *exc):
        self.events.append({'domain': 'example.com', 'method': 'CONNECT', 'bytes_in': 0,
                            'bytes_out': 0, 'decision': 'deny', 'job_id': self.job_id})
        lifecycle.append(('drain', self.job_id))
    monkeypatch.setattr(ProfileNetwork, '__aenter__', enter)
    monkeypatch.setattr(ProfileNetwork, '__aexit__', exit)
    transport = Transport(profile_config())
    job = profile_job(profile)
    original = dict(job)
    def submit(value, stop, *, proxy_url):
        lifecycle.append(('dispatch', value['execution_id']))
        token = proxy_url.split('bob:', 1)[1].split('@', 1)[0]
        return {'stdout_b64': base64.b64encode(token.encode()).decode(), 'stderr_b64': '',
                'cleanup': 'idle_proved', 'returncode': 0}
    monkeypatch.setattr(transport, '_submit', submit)
    receipt = transport.submit(job, None)
    assert lifecycle == [('enroll', 'job-one'), ('dispatch', 'job-one'), ('drain', 'job-one')]
    assert base64.b64decode(receipt['stdout_b64']) == b'[job-token]'
    assert receipt['network_log'][0]['decision'] == 'deny'
    assert receipt['network_profile'] == profile and job == original
    def fail(*args, **kwargs):
        raise OSError('transport stopped')
    monkeypatch.setattr(transport, '_submit', fail)
    with pytest.raises(OSError, match='transport stopped'):
        transport.submit(job, None)
    assert lifecycle[-1] == ('drain', 'job-one')


@pytest.mark.parametrize('profile', ['codex', 'mcp'])
def test_daemon_launch_gets_exact_proxy_environment(monkeypatch, profile):
    import io
    from contextlib import nullcontext
    from core.worker import daemon
    config = profile_config()
    captured = []
    monkeypatch.setattr(daemon, 'policy_ready', lambda c: None)
    monkeypatch.setattr(daemon, 'resolve_profile', lambda c, r: (['installed-cli'], Path.cwd(), True))
    monkeypatch.setattr(daemon.os, 'getpgrp', lambda: 1, raising=False)
    monkeypatch.setattr(daemon.tempfile, 'TemporaryDirectory', lambda **kw: nullcontext('/job-temp'))
    monkeypatch.setattr(daemon.tempfile, 'TemporaryFile', io.BytesIO)
    def launch(*args, **kwargs):
        captured.append(kwargs['env'])
        raise FileNotFoundError('portable fixture stops before exec')
    monkeypatch.setattr(daemon.subprocess, 'Popen', launch)
    monkeypatch.setenv('HTTPS_PROXY', 'http://evil.test')
    monkeypatch.setenv('NO_PROXY', '*')
    request = {'profile': profile, 'job_id': 'job-one', 'input': '', 'timeout': 10,
               'expires': time.time() + 10, 'workspace': 'scratch'}
    proxy = 'http://bob:' + 'a' * 64 + '@127.0.0.1:21004'
    conn = SimpleNamespace(sendall=lambda data: None)
    daemon.execute(conn, request, config, prevalidated=True, proxy_url=proxy)
    env = captured[0]
    assert all(env[k] == proxy for k in ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy'))
    assert env['NO_PROXY'] == env['no_proxy'] == ''
    assert ('CODEX_HOME' in env) == (profile == 'codex')
    from core.worker.contract import profile_proxy_env, WorkerUnavailable
    for invalid in (None, proxy.replace('21004', '21005'), proxy.replace('127.0.0.1', 'example.com')):
        with pytest.raises(WorkerUnavailable, match='requires a lane proxy lease'):
            profile_proxy_env(config, profile, invalid)
    with pytest.raises(WorkerUnavailable, match='metadata refused'):
        profile_proxy_env(config, 'shell', proxy)
    assert profile_proxy_env(config, 'shell', None) == {}


def test_profile_token_split_across_chunks_is_redacted_before_terminal_journal():
    from core.worker.daemon import ExecutionConnection
    token = 'a' * 64
    saved = []
    journal = SimpleNamespace(call=lambda *args, **kwargs: saved.append(kwargs))
    request = {'execution_id': 'job', 'request_digest': 'b' * 64, 'lane_generation': 1,
               'controller_session': 'controller'}
    conn = ExecutionConnection(SimpleNamespace(sendall=lambda data: None), journal, request, 'epoch', proxy_token=token)
    for raw in (b'prefix ' + token[:17].encode(), token[17:].encode() + b' suffix'):
        conn.sendall(json.dumps({'type': 'chunk', 'stream': 'stderr', 'data': base64.b64encode(raw).decode()}).encode())
    conn.sendall(b'{"type":"result","returncode":0}')
    assert base64.b64decode(saved[0]['receipt']['stderr_b64']) == b'prefix [job-token] suffix'


def test_transport_proxy_metadata_does_not_change_frozen_request(monkeypatch):
    from core.worker.client import Transport
    from core.worker.contract import request_digest
    sent = []
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def sendall(self, raw): sent.append(json.loads(raw))
    transport = Transport({})
    monkeypatch.setattr(transport, 'connect', Socket)
    monkeypatch.setattr(transport, 'negotiated', lambda *a: None)
    monkeypatch.setattr(transport, 'frame', lambda *a, **kw: {'type': 'result', 'returncode': 0})
    job = profile_job()
    job['request_digest'] = request_digest(job)
    proxy = 'http://bob:' + 'a' * 64 + '@127.0.0.1:21004'
    transport._submit(job, None, proxy_url=proxy)
    metadata = sent[0].pop('network_proxy')
    assert metadata == proxy and sent[0] == job
    assert request_digest(sent[0]) == job['request_digest']


async def test_profile_callbacks_return_fixed_decision_and_capture_job_receipt():
    from core.worker.profile_network import ProfileNetwork
    job = ProfileNetwork(profile_job(), profile_config())
    replies = []
    job.reader = asyncio.StreamReader()
    job.reader.feed_data(b'{"type":"policy","domain":"chatgpt.com","method":"CONNECT"}\n')
    job.reader.feed_data(b'{"type":"policy","domain":"pypi.org","method":"CONNECT"}\n')
    job.reader.feed_data(b'{"type":"log","domain":"chatgpt.com","method":"CONNECT",'
                         b'"bytes_in":12,"bytes_out":24,"decision":"allow","job_id":"job-one"}\n')
    job.reader.feed_eof()
    class Writer:
        def write(self, raw): replies.append(json.loads(raw))
        async def drain(self): pass
    job.writer = Writer()
    await job.serve()
    assert replies == [{'decision': 'allow'}, {'decision': 'deny'}, {'saved': True}]
    assert job.approvals == [] and job.events[0]['profile'] == 'codex'
    assert job.events[0]['bytes_out'] == 24


@pytest.mark.parametrize('profile', ['codex', 'mcp'])
def test_peer_checked_daemon_dispatch_accepts_ephemeral_proxy_without_changing_job(monkeypatch, profile):
    import struct
    import socket
    from core.worker import daemon
    from core.worker.contract import profile_revision, request_digest
    config = {**profile_config(), 'service_uid': 123}
    state = {'lane_generation': 1, 'controller_session': 'controller', 'daemon_epoch': 'epoch'}
    journal_calls, frames, launches = [], [], []
    def journal_call(op, **kwargs):
        journal_calls.append((op, kwargs))
        if op in {'boot', 'fence'}: return dict(state)
        if op == 'inspect': return {'state': 'unknown_history'}
        if op == 'admit': return {'state': 'new'}
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def getsockopt(self, *args): return struct.pack('3i', 1, 123, 123)
        def sendall(self, raw): frames.append(json.loads(raw))
    conn = Connection()
    server = daemon.ProtocolServer(config, SimpleNamespace(accept=lambda: (conn, None)),
                                   SimpleNamespace(call=journal_call))
    server.barrier = (1, 'epoch')
    job = dict(version=2, op='submit', execution_id='job-one', logical_job_id='logical-job',
               profile=profile, profile_revision=profile_revision(config['profiles'][profile]),
               workspace='scratch', cwd='.', args=[], input='original input', timeout=120,
               output_limit=1024, lane_generation=1, controller_session='controller',
               not_after=time.time()+120, submit_expires_at=time.time()+10)
    job['request_digest'] = request_digest(job)
    proxy = 'http://bob:' + 'a' * 64 + '@127.0.0.1:21004'
    inputs = iter([{'version': 2, 'op': 'hello'}, {**job, 'network_proxy': proxy}])
    monkeypatch.setattr(socket, 'SO_PEERCRED', getattr(socket, 'SO_PEERCRED', 17), raising=False)
    monkeypatch.setattr(server, 'read', lambda conn: next(inputs))
    monkeypatch.setattr(server, 'hello', lambda: {'type': 'hello', 'version': 2})
    monkeypatch.setattr(daemon, 'policy_ready', lambda cfg: None)
    def execute(owner, request, cfg, **kwargs):
        launches.append((request, kwargs['proxy_url']))
        daemon.send(owner, {'type': 'result', 'returncode': 0})
    monkeypatch.setattr(daemon, 'execute', execute)
    server.accept()
    assert launches[0][0]['input'] == 'original input' and launches[0][1] == proxy
    assert 'network_proxy' not in launches[0][0]
    assert frames[-1]['request_digest'] == job['request_digest'] and frames[-1]['outcome'] == 'succeeded'
    assert all('network_proxy' not in json.dumps(kwargs) and proxy not in json.dumps(kwargs)
               for _, kwargs in journal_calls)


@pytest.fixture
async def network_pg():
    # Same disposable local-schema discipline as gateway/test_day_one_postgres.
    dsn = os.environ.get('S4B_TEST_POSTGRES_DSN')
    if not dsn:
        pytest.skip('S4B_TEST_POSTGRES_DSN not set; real Postgres required')
    assert urlparse(dsn).hostname in {None, 'localhost', '127.0.0.1', '::1'}
    import asyncpg
    schema = 's1b_' + uuid.uuid4().hex
    admin = await asyncpg.connect(dsn, timeout=5)
    await admin.execute('CREATE SCHEMA "' + schema + '"')
    pool = None
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6,
            server_settings={'search_path': schema}, command_timeout=5)
        await pool.execute('CREATE TABLE projects(id UUID PRIMARY KEY,user_id TEXT,is_archived BOOLEAN DEFAULT FALSE);'
                           'CREATE TABLE conversations(id UUID PRIMARY KEY,user_id TEXT,project_id UUID)')
        await Store(pool, 'lane').initialize()
        yield pool
    finally:
        if pool:
            await pool.close()
        await admin.execute('DROP SCHEMA "' + schema + '" CASCADE')
        await admin.close()


async def test_allow_project_persists_scoped_owner_edit_and_receipts(network_pg, monkeypatch):
    pool = network_pg
    project, conversation = uuid.uuid4(), uuid.uuid4()
    await pool.execute('INSERT INTO projects(id,user_id) VALUES($1,$2)', project, 'owner')
    await pool.execute('INSERT INTO conversations VALUES($1,$2,$3)', conversation, 'owner', project)
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    owner = replace(auth, token_type='human', session_kind='owner_stepup',
                    allowed_operations=('builder.approve', 'builder.network.set'))
    from core.execution import owner as owner_module
    async def require(a, op):
        if a.session_kind != 'owner_stepup' or op not in a.allowed_operations:
            raise ExecutionError('owner_required', 403)
    monkeypatch.setattr(owner_module, 'require_owner', require)
    builder = Builder(pool)
    async def policy(command='curl https://example.com', host='example.com', method='CONNECT'):
        return await builder.network.policy(auth, conversation, command, 'job-one', host, method)
    pending = await policy()
    assert pending == await policy()
    card = pending['approval']
    assert card['kind'] == 'network' and card['detail']['domain'] == 'example.com'
    args = {'approval_id': pending['approval_id'], 'decision': 'allow_project', 'request_key': 'allow-project'}
    with pytest.raises(ExecutionError, match='owner_required'):
        await builder.approve(auth, args)
    assert (await builder.approve(owner, args))['state'] == 'allowed'
    assert (await builder.approve(owner, args))['state'] == 'allowed'
    builder = Builder(pool)  # New service instance, persisted project allowlist.
    config = await builder.network.project(auth, project)
    assert config['project_extra'] == ['example.com'] and config['revision'] == 1
    assert (await policy(command='different command'))['decision'] == 'allow'
    assert (await policy(method='POST'))['decision'] == 'ask'
    with pytest.raises(ExecutionError, match='project_not_found'):
        await builder.network.project(replace(auth, owner='other'), project)
    edit = {'project_id': str(project), 'remove': ['example.com'], 'expected_revision': 1, 'request_key': 'remove'}
    assert (await builder.network.set_project(owner, edit))['project_extra'] == []
    assert (await builder.network.set_project(owner, edit))['revision'] == 2
    with pytest.raises(ExecutionError, match='revision_conflict'):
        await builder.network.set_project(owner, {**edit, 'request_key': 'stale'})
    row = await builder.store.mode(auth, str(conversation))
    await builder.network.log(auth, row, 'job-one', {'domain': 'example.com', 'method': 'CONNECT',
        'bytes_in': 10, 'bytes_out': 20, 'decision': 'allow', 'approval_id': pending['approval_id']})
    assert (await builder.network.logs(auth, {'job_id': 'job-one'}))['items'][0]['bytes_out'] == 20
    assert (await builder.network.logs(replace(auth, lane='other'), {'job_id': 'job-one'}))['items'] == []


@pytest.mark.parametrize('decision', ['allow_once', 'allow_session', 'deny'])
async def test_network_approval_retry_and_method_binding(network_pg, monkeypatch, decision):
    pool = network_pg
    project, conversation = uuid.uuid4(), uuid.uuid4()
    await pool.execute('INSERT INTO projects(id,user_id) VALUES($1,$2)', project, 'owner')
    await pool.execute('INSERT INTO conversations VALUES($1,$2,$3)', conversation, 'owner', project)
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    from core.execution import owner
    monkeypatch.setattr(owner, 'require_owner', AsyncMock())
    builder = Builder(pool)
    async def request(method='GET', command='curl example.com'):
        return await builder.network.policy(auth, conversation, command, 'job', 'example.com', method)
    pending = await request()
    await builder.approve(auth, {'approval_id': pending['approval_id'], 'decision': decision, 'request_key': 'decision'})
    assert (await request())['decision'] == ('deny' if decision == 'deny' else 'allow')
    assert (await request())['decision'] == {'deny': 'deny', 'allow_once': 'ask', 'allow_session': 'allow'}[decision]
    assert (await request(method='POST'))['decision'] == 'ask'
    assert (await request(command='another command'))['decision'] == ('allow' if decision == 'allow_session' else 'ask')
