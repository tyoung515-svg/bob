"""Real SQLite/HTTP admission, epoch revocation and credential-free broker results."""
import asyncio
import base64
import json
import secrets

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pyotp
import pytest

from auth import create_access_token, create_agent_token, decode_access_token, auth_middleware
from config import config
from core import credential_state
from core.recovery_contract import digest, validate
from core.settings_contract import SettingsError
from db import create_refresh_token, validate_and_rotate_refresh_token, _sqlite_connection
import credential_auth
from routers import recovery, auth_routes
from tests.test_password_change import password_h
from tests.test_owner_sessions import owner_h, stepup
from tests.test_execution_broker_v2 import broker
from tests.test_owner_broker import owner_transport


@pytest.fixture
async def factors_h(password_h):
    h = password_h
    app = web.Application(middlewares=[auth_middleware])
    app.add_routes(recovery.router)
    app.add_routes(auth_routes.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    h.factors_client = client
    try:
        yield h
    finally:
        await client.close()


def prepared(h, pair='password_totp', key='prepare'):
    value = dict(contract_version=2, request_key=key, pair=pair,
                 replacement={'recovery_id':'fresh', 'verifier':'a'*64})
    if pair in {'password_totp', 'password_stick'}:
        value['password'] = config.BOBCLAW_PASSWORD
    if pair in {'password_totp', 'totp_stick'}:
        value['totp'] = pyotp.TOTP(config.TOTP_SECRET).at(h.now[0])
    if pair in {'password_stick', 'totp_stick'}:
        value['old_stick'] = dict(installation_id='install', lane_id='lane', owner='owner',
                                  recovery_id='old', secret=base64.b32encode(b'x'*32).decode().rstrip('='))
    if pair in {'password_stick', 'setup'}:
        value['new_seed'] = 'A'*32
    if pair == 'totp_stick':
        value['new_password'] = 'new-password-CANARY'
    if pair == 'setup':
        value['setup_capability'] = 'CAPABILITY-CANARY'
    return value


@pytest.mark.parametrize('pair', ['password_stick','password_totp','totp_stick','setup'])
async def test_pairs_forward_only_verified_assertions_and_project_reply(factors_h, monkeypatch, caplog, pair):
    h = factors_h
    calls = []
    def helper(value):
        calls.append(value)
        return dict(challenge_id='challenge', expires_at=int(h.now[0])+300,
                    state='proof_verified', secret='CANARY', seed='A'*32)
    monkeypatch.setattr(recovery.settings_client, 'call', helper)
    body = prepared(h, pair)
    response = await h.factors_client.post('/settings/factors/prepare', headers=h.headers, json=body)
    assert response.status == 200, await response.text()
    assert await response.json() == dict(challenge_id='challenge', expires_at=int(h.now[0])+300, state='proof_verified')
    assert len(calls) == 1
    assert 'password' not in calls[0] and 'totp' not in calls[0]
    assert calls[0]['expected_epoch'] == 1 and len(calls[0]['context']) == 64
    assert 'CANARY' not in caplog.text
    repeat = await h.factors_client.post('/settings/factors/prepare', headers=h.headers, json=body)
    assert repeat.status == 409 and len(calls) == 1


async def test_wrong_factors_limits_and_old_totp_spend_survive_restart(factors_h, monkeypatch):
    h = factors_h
    calls = []
    monkeypatch.setattr(recovery.settings_client, 'call', lambda value: calls.append(value) or
        dict(challenge_id='challenge', expires_at=int(h.now[0])+300, state='proof_verified'))
    value = prepared(h)
    bad = await h.factors_client.post('/settings/factors/prepare', headers=h.headers, json={**value,'password':'wrong'})
    assert bad.status == 422 and not calls
    await credential_auth.initialize()
    limited = await h.factors_client.post('/settings/factors/prepare', headers={**h.headers,'X-Bob-Broker-Link':'other'},
        json=prepared(h, key='different'))
    assert limited.status == 429 and 'Retry-After' in limited.headers
    h.now[0] += 31
    assert (await h.factors_client.post('/settings/factors/prepare', headers=h.headers, json=prepared(h, key='valid'))).status == 200
    await credential_auth.initialize()
    spent = await h.factors_client.post('/settings/factors/prepare', headers=h.headers, json=prepared(h, key='spent'))
    assert spent.status == 422 and len(calls) == 1


async def test_concurrent_totp_proof_once_and_context_denials(factors_h, monkeypatch):
    h = factors_h
    calls = []
    monkeypatch.setattr(recovery.settings_client, 'call', lambda value: calls.append(value) or
        dict(challenge_id='challenge', expires_at=int(h.now[0])+300, state='proof_verified'))
    replies = await asyncio.gather(*(h.factors_client.post('/settings/factors/prepare', headers=h.headers,
        json=prepared(h,key=str(i))) for i in range(2)))
    assert sorted(r.status for r in replies) == [200,422] and len(calls) == 1
    for token in ('', create_access_token(), create_agent_token()):
        response = await h.factors_client.post('/settings/factors/prepare', headers={'Authorization':'Bearer '+token}, json=prepared(h))
        assert response.status == 404


async def test_commit_admission_precedes_restart_lost_reply_no_replay(factors_h, monkeypatch, caplog):
    h = factors_h
    calls = []
    def lost(value):
        calls.append(value)
        h.active.update(epoch=2,revocation_epoch=2,reset_at='2026-11-04T00:00:00+00:00')
        raise SettingsError('CANARY-SEED')
    monkeypatch.setattr(recovery.settings_client, 'call', lost)
    body = dict(contract_version=2, request_key='commit', challenge_id='challenge', material_saved=True,new_totp='')
    response = await h.factors_client.post('/settings/factors/commit', headers=h.headers,json=body)
    assert response.status == 503 and 'CANARY' not in await response.text()
    async with _sqlite_connection() as db:
        async with db.execute('SELECT request_key FROM credential_requests') as cursor:
            assert [r[0] for r in await cursor.fetchall()] == ['commit']
    assert len(calls) == 1 and 'CANARY' not in caplog.text


@pytest.mark.parametrize('code,counted', [('settings_unavailable', False), ('settings_failed', False),
                                           ('recovery_busy', False), ('invalid_factors', True)])
async def test_only_refused_factors_count_toward_backoff(factors_h, monkeypatch, code, counted):
    # Installed regression: the helper restarts this gateway during a successful
    # commit, so the in-flight handler sees a broken reply. That must not
    # throttle the owner's next attempt.
    h = factors_h
    def helper(value):
        if value['op'] == 'factors.commit':
            raise SettingsError(code)
        return dict(challenge_id='challenge', expires_at=int(h.now[0])+300, state='proof_verified')
    monkeypatch.setattr(recovery.settings_client, 'call', helper)
    body = dict(contract_version=2, request_key='commit-'+code, challenge_id='challenge', material_saved=True, new_totp='')
    response = await h.factors_client.post('/settings/factors/commit', headers=h.headers, json=body)
    assert response.status != 200
    async with _sqlite_connection() as db:
        async with db.execute('SELECT COUNT(*) FROM owner_attempts') as cursor:
            assert ((await cursor.fetchone())[0] > 0) is counted
    following = await h.factors_client.post('/settings/factors/prepare', headers=h.headers, json=prepared(h, key='next-'+code))
    assert following.status == (429 if counted else 200), await following.text()


async def test_recovery_revokes_all_token_classes_and_notice_after_login(factors_h, monkeypatch):
    h = factors_h
    human = create_access_token()
    agent = create_agent_token()
    refresh = await create_refresh_token()
    owner, _ = await stepup(h.client, h.headers)
    h.active.update(epoch=2,revocation_epoch=2,reset_at='2026-11-04T00:00:00+00:00')
    for token in (human,agent,h.headers['Authorization'][7:]):
        assert decode_access_token(token) is None
    assert (await h.client.get('/auth/owner/status',headers=owner)).status == 503
    monkeypatch.setenv('BOB_CREDENTIAL_EPOCH','2')
    await credential_auth.initialize()
    for token in (human,agent,h.headers['Authorization'][7:]):
        assert decode_access_token(token) is None
    assert await validate_and_rotate_refresh_token(refresh) is None
    assert (await h.client.get('/auth/owner/status',headers=owner)).status == 403
    assert decode_access_token(create_agent_token())['service_epoch'] == 2
    h.now[0] += 31
    response = await h.factors_client.post('/auth/login',json={'password':config.BOBCLAW_PASSWORD,
        'totp_code':pyotp.TOTP(config.TOTP_SECRET).at(h.now[0])})
    assert response.status == 200
    login = await response.json()
    assert login['security_notice'] == 'two-factor was reset on 2026-11-04'
    assert decode_access_token(login['access_token'])['sub'] == 'owner'


@pytest.mark.parametrize('token_class', ['portal','agent','human'])
async def test_idle_and_active_websockets_revoked(factors_h, token_class):
    from auth import CredentialWebSocketResponse, authenticate_ws, create_portal_token
    from aiohttp import WSMsgType
    h = factors_h
    async def echo(request):
        ws = CredentialWebSocketResponse()
        await ws.prepare(request)
        claims, _ = await authenticate_ws(request,ws,allow_agent=True)
        if claims:
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await ws.send_str(message.data)
        return ws
    app = web.Application()
    app.router.add_get('/ws/chat',echo)
    client = TestClient(TestServer(app))
    await client.start_server()
    token = {'portal':lambda:create_portal_token('owner',{}),'agent':create_agent_token,'human':create_access_token}[token_class]()
    try:
        ws = await client.ws_connect('/ws/chat',headers={'Authorization':'Bearer '+token})
        await ws.send_str('before')
        assert (await ws.receive(timeout=2)).data == 'before'
        h.active.update(epoch=2,revocation_epoch=2)
        assert (await ws.receive(timeout=2)).type in {WSMsgType.CLOSE,WSMsgType.CLOSED}
        await ws.close()
    finally:
        await client.close()


def test_verifier_domain_binding_and_noncanonical_secret_rejected():
    binding = dict(installation_id='install',lane_id='lane',owner='owner')
    secret = base64.b32encode(b'x'*32).decode().rstrip('=')
    reference = digest(binding,'id',secret)
    assert all(digest({**binding,k:'other'},'id',secret) != reference for k in binding)
    assert digest(binding,'other',secret) != reference
    with pytest.raises(SettingsError):
        digest(binding,'id','A'*51+'B')


def test_broker_safe_factor_projection_and_lost_reply_once(broker,monkeypatch):
    module,instance,_ = broker
    if not hasattr(instance,'op_factors_prepare'):
        pytest.skip('Sibling app half has not applied S3G; maintained source and snapshot required')
    owner_transport(module,instance,monkeypatch)
    instance._require_owner_operation = lambda op: None
    calls=[]
    def rest(method,path,**kwargs):
        calls.append((method,path,kwargs))
        if path.endswith('prepare'):
            return dict(challenge_id='safe',expires_at=123,state='proof_verified',secret='CANARY',seed='A'*32)
        raise RuntimeError('CANARY '+ 'A'*32)
    instance.transport.rest=rest
    args=dict(contract_version=2,request_key='once',pair='setup',replacement={'recovery_id':'new','verifier':'a'*64},
              setup_capability='CANARY',new_seed='A'*32)
    assert instance.op_factors_prepare(args) == dict(challenge_id='safe',expires_at=123,state='proof_verified')
    commit=dict(contract_version=2,request_key='commit',challenge_id='safe',material_saved=True,new_totp='123456')
    with pytest.raises(module.BrokerError) as caught:
        instance.op_factors_commit(commit)
    assert caught.value.code == 'recovery_outcome_unknown' and 'CANARY' not in caught.value.detail
    with pytest.raises(module.BrokerError,match='request_key_used'):
        instance.op_factors_commit(commit)
    assert len(calls)==2 and not module._OWNER_SECRETS


def test_broker_renewal_cannot_cross_recovery_epoch(broker,monkeypatch):
    import jwt
    module,instance,_=broker
    if not hasattr(instance,'op_factors_prepare'):
        pytest.skip('Sibling app half is separate')
    before=jwt.encode({'sub':'owner','token_type':'portal','service_epoch':0},'fixture-recovery-key-at-least-32-characters',algorithm='HS256')
    after=jwt.encode({'sub':'owner','token_type':'portal','service_epoch':2},'fixture-recovery-key-at-least-32-characters',algorithm='HS256')
    monkeypatch.setattr(module,'load_token',lambda path:after)
    transport=module.LiveTransport('http://127.0.0.1:7836',before,token_file='unused')
    with pytest.raises(module.BrokerError,match='credential_epoch_changed'):
        transport._bearer()
    assert transport._token==before


async def test_mint_cannot_stamp_new_epoch_after_old_generation_proof(factors_h,monkeypatch):
    from auth import create_portal_token
    h=factors_h
    for mint in (create_agent_token,lambda:create_portal_token('owner',{})):
        reads=iter([{'epoch':1,'revocation_epoch':0},{'epoch':2,'revocation_epoch':2}])
        monkeypatch.setattr(credential_state,'active',lambda:next(reads))
        with pytest.raises(SettingsError,match='credential_generation_mismatch'):
            mint()
