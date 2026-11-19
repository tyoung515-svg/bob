"""3.3 real SQLite/HTTP and source/packaged broker; no providers or patch reads."""
import asyncio
import json
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pyotp
import pytest

from auth import auth_middleware, create_access_token, decode_access_token, create_portal_token
from config import config
from core import credential_state
from core.settings_contract import SettingsError, password_rules, password_result, status_result, validate
from db import _sqlite_connection, create_refresh_token, validate_and_rotate_refresh_token
import credential_auth
from routers import settings, auth_routes
from tests.test_owner_sessions import owner_h, stepup
from tests.test_execution_broker_v2 import broker
from tests.test_owner_broker import owner_transport


@pytest.mark.parametrize('value', ['short', 'x'*1025, 'tenletters\n', 'tenletters\x00', 'tenletters\u2028', 'tenletters\u200b', 42])
def test_password_rules_reject_without_echo(value):
    with pytest.raises(SettingsError, match='invalid_password'):
        password_rules(value)


@pytest.mark.parametrize('field', ['lane', 'owner', 'path', 'unit', 'uid', 'env', 'gateway_url', 'current', 'totp'])
def test_password_helper_only_accepts_verified_fixed_mutation(field):
    request = dict(version=1, op='password.change', new='new-password-canary', expected_epoch=1, request_key='once')
    with pytest.raises(SettingsError, match='invalid_args'):
        validate({**request, field: 'CANARY'})
    with pytest.raises(SettingsError, match='unsupported_operation'):
        validate(request, receipt=True)


def test_password_projection_and_env_serialization(monkeypatch):
    from pathlib import Path
    import importlib.util
    path = Path(__file__).resolve().parents[2] / 'deploy/runtime/lib/credentials.py'
    spec = importlib.util.spec_from_file_location('password_runtime', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import sys
    # The pure serializer has no filesystem/syscall dependencies.
    common_path = path.with_name('common.py')
    spec = importlib.util.spec_from_file_location('password_common', common_path)
    common = importlib.util.module_from_spec(spec)
    # common imports pwd, a Linux-only identity module.
    if __import__('os').name == 'nt':
        monkeypatch.setitem(sys.modules, 'pwd', SimpleNamespace())
    spec.loader.exec_module(common)
    monkeypatch.setitem(sys.modules, 'common', common)
    value = 'new-pass-"\\$()`%n#= Unicode 密碼'
    payload = module.environment({'password': value, 'epoch': 2})
    assert payload.count('\n') == 2
    assert payload.startswith('BOBCLAW_PASSWORD="') and 'BOB_CREDENTIAL_EPOCH="2"' in payload
    import shlex
    assert shlex.split(payload.splitlines()[0].split('=', 1)[1])[0] == value
    for changed, restarted in [(False, False), (True, False), (True, True)]:
        result = {'changed': changed, 'restarted': restarted}
        assert password_result({**result, 'password': 'CANARY'}) == result
        for phase in ('ready', 'failed'):
            assert status_result(dict(operation='password.change', phase=phase, operation_id='safe', result=result)) == {
                'operation_id': 'safe', 'phase': phase, 'result': result}
    with pytest.raises(SettingsError):
        password_result({'changed': False, 'restarted': True})


@pytest.fixture
async def password_h(owner_h, monkeypatch):
    h = owner_h
    active = {'epoch': 1}
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a'*12)
    monkeypatch.setenv('BOB_CREDENTIAL_EPOCH', '1')
    monkeypatch.setattr(credential_state, 'active', lambda: dict(active))
    monkeypatch.setattr(settings.settings_client, 'unavailable_reason', lambda: None)
    monkeypatch.setattr(credential_auth.time, 'time', lambda: h.now[0])
    app = web.Application(middlewares=[auth_middleware])
    app.add_routes(settings.router)
    app.add_routes(auth_routes.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    h.password_client, h.active = client, active
    try:
        yield h
    finally:
        await client.close()


def body(h, key='once'):
    return dict(contract_version=2, current=config.BOBCLAW_PASSWORD, new='new-password-canary',
                totp=pyotp.TOTP(config.TOTP_SECRET).at(h.now[0]), request_key=key)


@pytest.mark.parametrize('changed,restarted', [(False, False), (True, False), (True, True)])
async def test_combined_form_exact_reply_one_proof_and_single_use(password_h, monkeypatch, changed, restarted):
    h = password_h
    calls = []
    def helper(value):
        calls.append(value)
        return dict(changed=changed, restarted=restarted, secret='DO-NOT-LEAK')
    monkeypatch.setattr(settings.settings_client, 'call', helper)
    response = await h.password_client.post('/settings/password', headers=h.headers, json=body(h))
    assert response.status == 200 and await response.json() == dict(changed=changed, restarted=restarted)
    assert calls == [dict(version=1, op='password.change', new='new-password-canary', expected_epoch=1, request_key='once')]
    response = await h.password_client.post('/settings/password', headers=h.headers, json=body(h))
    assert response.status == 409 and (await response.json())['error']['code'] == 'request_key_used'
    assert len(calls) == 1
    # Different admission key cannot spend the same factor proof.
    response = await h.password_client.post('/settings/password', headers=h.headers, json=body(h, 'other'))
    assert await response.json() == dict(changed=False, restarted=False) and len(calls) == 1
    async with _sqlite_connection() as db:
        async with db.execute('SELECT * FROM credential_requests') as c:
            assert [r[0] for r in await c.fetchall()] == ['once', 'other']


async def test_concurrent_proof_one_winner_and_durable_throttle(password_h, monkeypatch):
    h = password_h
    calls = []
    monkeypatch.setattr(settings.settings_client, 'call', lambda value: calls.append(value) or dict(changed=True, restarted=False))
    replies = await asyncio.gather(*(h.password_client.post('/settings/password', headers=h.headers,
                                       json=body(h, str(n))) for n in range(2)))
    results = [await r.json() for r in replies]
    assert len(calls) == 1 and sum(r.get('changed') is True for r in results) == 1
    response = await h.password_client.post('/settings/password', headers={**h.headers, 'X-Bob-Broker-Link': 'different'}, json=body(h, 'retry'))
    assert response.status == 429 and 1 <= int(response.headers['Retry-After']) <= 301
    await credential_auth.initialize()  # A restart must not clear aggregate backoff.
    response = await h.password_client.post('/settings/password', headers=h.headers, json=body(h, 'after-restart'))
    assert response.status == 429
    h.now[0] += 31
    response = await h.password_client.post('/settings/password', headers=h.headers, json=body(h, 'fresh'))
    assert response.status == 200 and (await response.json())['changed']


async def test_commit_fences_human_refresh_owner_and_gateway_mismatch(password_h, monkeypatch):
    h = password_h
    human = create_access_token()
    refresh = await create_refresh_token()
    owner, _ = await stepup(h.client, h.headers)
    h.active['epoch'] = 2
    assert decode_access_token(human) is None
    response = await h.password_client.get('/auth/generation')
    assert response.status == 503
    response = await h.client.get('/auth/owner/status', headers=owner)
    assert response.status == 503
    monkeypatch.setenv('BOB_CREDENTIAL_EPOCH', '2')
    await credential_auth.initialize()
    assert decode_access_token(human) is None
    assert await validate_and_rotate_refresh_token(refresh) is None
    assert (await h.client.get('/auth/owner/status', headers=owner)).status == 403
    assert decode_access_token(h.headers['Authorization'][7:])['token_type'] == 'portal'
    assert decode_access_token(create_access_token())['credential_epoch'] == 2


@pytest.mark.parametrize('send_after', [False, True])
async def test_open_human_websocket_closed_without_successful_gateway_restart(password_h, send_after):
    from auth import CredentialWebSocketResponse, authenticate_ws
    from aiohttp import WSMsgType
    h = password_h
    accepted = []
    async def echo(request):
        ws = CredentialWebSocketResponse()
        await ws.prepare(request)
        claims, _ = await authenticate_ws(request, ws)
        if claims is None:
            return ws
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                accepted.append(message.data)
                await ws.send_str(message.data)
        return ws
    app = web.Application()
    app.router.add_get('/ws', echo)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        ws = await client.ws_connect('/ws', headers={'Authorization':'Bearer '+create_access_token()})
        await ws.send_str('before')
        assert (await ws.receive(timeout=2)).data == 'before'
        h.active['epoch'] = 2
        if send_after:
            await ws.send_str('after')
        message = await ws.receive(timeout=2)
        assert message.type in {WSMsgType.CLOSE, WSMsgType.CLOSED}
        assert accepted == ['before']
        await ws.close()
    finally:
        await client.close()


async def test_login_throttle_durable_and_shared_totp_with_stepup(password_h):
    h = password_h
    response = await h.password_client.post('/auth/login', json={'password': 'bad', 'totp_code': '000000'})
    assert response.status == 401
    await credential_auth.initialize()
    response = await h.password_client.post('/auth/login', json={'password': config.BOBCLAW_PASSWORD, 'totp_code': body(h)['totp']})
    assert response.status == 429 and 'Retry-After' in response.headers
    response = await h.password_client.post('/settings/password', headers=h.headers, json=body(h, 'cross-operation'))
    assert response.status == 429  # Aggregate owner limit also covers other factors' routes.
    response = await h.client.post('/auth/owner/begin', headers=h.headers,
        json={'operation':'secrets.set','resource':{'provider':'deepseek'}})
    challenge = await response.json()
    response = await h.client.post('/auth/owner/confirm', headers=h.headers,
        json={'challenge_id':challenge['challenge_id'],'password':config.BOBCLAW_PASSWORD,'totp':body(h)['totp']})
    assert response.status == 429
    h.now[0] += 31
    response = await h.password_client.post('/auth/login', json={'password': config.BOBCLAW_PASSWORD, 'totp_code': body(h)['totp']})
    assert response.status == 200
    response = await h.password_client.post('/settings/password', headers=h.headers, json=body(h))
    assert await response.json() == dict(changed=False, restarted=False)


async def test_wrong_context_rules_and_lost_commit_reply_never_claims_unchanged(password_h, monkeypatch, caplog):
    h = password_h
    for headers in ({}, {'Authorization': 'Bearer ' + create_access_token()},
                    {'Authorization': 'Bearer ' + create_portal_token('foreign', {}), 'X-Bob-Broker-Link': 'link'}):
        assert (await h.password_client.post('/settings/password', headers=headers, json=body(h))).status == 404
    for new in ('short', config.BOBCLAW_PASSWORD, 'canary\nINJECT=1'):
        response = await h.password_client.post('/settings/password', headers=h.headers, json={**body(h), 'new': new})
        assert response.status == 422
    def dropped(value):
        h.active['epoch'] = 2
        raise RuntimeError('new-password-canary')
    monkeypatch.setattr(settings.settings_client, 'call', dropped)
    response = await h.password_client.post('/settings/password', headers=h.headers, json=body(h))
    assert response.status == 503 and 'changed' not in await response.text()
    assert 'new-password-canary' not in caplog.text


def test_broker_combined_form_lost_reply_safe_errors_no_resend(broker, monkeypatch):
    module, instance, _ = broker
    if not hasattr(instance, 'op_password_change'):
        pytest.skip('Sibling app not yet upgraded; source and packaged copies required')
    calls, _, _, _ = owner_transport(module, instance, monkeypatch)
    args = dict(contract_version=2, current='old-password-canary', new='new-password-canary', totp='123456', request_key='once')
    native = instance.transport.rest
    def lost(method, path, **kwargs):
        if path == '/settings/password':
            calls.append((method, path, kwargs))
            raise module.BrokerError('transport_unavailable', 'new-password-canary 123456')
        return native(method, path, **kwargs)
    instance.transport.rest = lost
    with pytest.raises(module.BrokerError) as caught:
        instance.op_password_change(args)
    assert caught.value.code == 'password_outcome_unknown'
    assert 'canary' not in caught.value.detail and '123456' not in caught.value.detail
    assert sum(p == '/settings/password' for _, p, _ in calls) == 1
    with pytest.raises(module.BrokerError) as reused:
        instance.op_password_change(args)
    assert reused.value.code == 'request_key_used'
    assert sum(p == '/settings/password' for _, p, _ in calls) == 1
    assert not any(p == '/auth/owner/confirm' for _, p, _ in calls)
    assert not module._OWNER_SECRETS and instance._owner_transport is None


@pytest.mark.parametrize('phase,result', [('prepared',None), ('committed',None), ('restart-pending',None),
    ('ready',dict(changed=True,restarted=True)), ('failed',dict(changed=True,restarted=False)),
    ('failed',dict(changed=False,restarted=False))])
def test_broker_safe_receipt_uses_only_unix_socket_without_gateway(broker, monkeypatch, phase, result):
    import io
    import stat
    import struct
    import sys
    module, instance, _ = broker
    if not hasattr(instance, 'op_password_change'):
        pytest.skip('Sibling app not yet upgraded; source and packaged copies required')
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a'*12)
    value = dict(operation_id='safe', phase=phase, secret='CANARY')
    if result:
        value['result'] = {**result, 'password':'CANARY'}
    sent = []
    class Listener:
        parents = ()
        def __truediv__(self, name): return self
        def lstat(self): return SimpleNamespace(st_uid=0, st_mode=stat.S_IFSOCK|0o660)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, value): pass
        def connect(self, value): pass
        def getsockopt(self, *args): return struct.pack('3i',1,0,0)
        def sendall(self, value): sent.append(json.loads(value))
        def makefile(self, *args): return io.BytesIO(json.dumps(value).encode()+b'\n')
    monkeypatch.setattr(module, 'Path', lambda path: Listener())
    monkeypatch.setitem(sys.modules, 'socket', SimpleNamespace(socket=lambda *a:Connection(), AF_UNIX=1, SOL_SOCKET=1, SO_PEERCRED=17))
    instance._rest = lambda *a, **k: pytest.fail('Receipt must not contact gateway')
    instance._owner_rest = instance._rest
    safe = instance.op_settings_status({'request_key':'once'})
    assert safe == {**{k:value[k] for k in ('operation_id','phase')}, **({'result':result} if result else {})}
    assert sent == [dict(version=1,op='status',request_key='once')]
    assert 'CANARY' not in json.dumps(safe)
