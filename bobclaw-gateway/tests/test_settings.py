"""3.2 closed schemas, real owner HTTP, secret projection and broker failures."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import uuid

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from core.settings_contract import SettingsError, fingerprint, secret_result, status_result, validate, decode
from core.execution.policy import ceiling
from core.permissions import Scope, evaluate_action
from routers import settings
from tests.test_owner_sessions import owner_h, stepup
from tests.test_execution_broker_v2 import broker
from tests.test_owner_broker import owner_transport

CANARY = 's3d-CANARY-DO-NOT-ECHO-123'
REQUEST = {'version': 1, 'op': 'secrets.set', 'provider': 'deepseek', 'key': CANARY, 'request_key': 'once'}


@pytest.fixture
def tmp_path(tmp_path_factory):
    if os.name != 'nt':
        return tmp_path_factory.mktemp('settings')
    path = Path(__file__).parents[1] / 'test-db' / ('settings-' + uuid.uuid4().hex)
    path.mkdir(parents=True, mode=0o777)
    return path


@pytest.mark.parametrize('field', ['lane', 'owner', 'path', 'root', 'unit', 'uid', 'gid', 'env', 'environment', 'command', 'argv', 'gateway_url'])
def test_helper_rejects_every_override(field):
    with pytest.raises(SettingsError, match='invalid_args'):
        validate({**REQUEST, field: CANARY})


@pytest.mark.parametrize('op', ['stage', 'commit', 'credentials.stage', 'credentials.commit', 'restart', 'secrets.get', 'shell'])
def test_future_or_unsafe_operations_are_not_installed(op):
    with pytest.raises(SettingsError):
        validate({**REQUEST, 'op': op})


@pytest.mark.parametrize('provider', ['openai', 'ANTHROPIC_API_KEY', '../deepseek', 'moonshot', 'openrouter'])
def test_unverified_storage_adapters_are_refused(provider):
    with pytest.raises(SettingsError, match='unsupported_provider'):
        validate({**REQUEST, 'provider': provider})


@pytest.mark.parametrize('key', ['', 'abc\n', 'abc\r', 'abc\x00', 'abc\u200b', 'x'*16385, 4],
                         ids=['empty', 'lf', 'cr', 'nul', 'format', 'oversize', 'non-string'])
def test_invalid_keys_do_not_echo_values(key):
    with pytest.raises(SettingsError) as caught:
        validate({**REQUEST, 'key': key})
    assert str(caught.value) == 'invalid_key'


@pytest.mark.parametrize('provider', ['anthropic', 'deepseek', 'google', 'glm', 'kimi', 'qwen'])
def test_each_key_provider_validates_and_projects(provider):
    validate({**REQUEST, 'provider': provider})
    result = secret_result(dict(provider=provider, stored=True, fingerprint=fingerprint(CANARY),
                                operation_id='safe', key=CANARY))
    assert result['provider'] == provider and CANARY not in json.dumps(result)


def test_key_capabilities_keep_ui_gate_and_advertise_enrollment(monkeypatch):
    from core import execution
    monkeypatch.setattr(execution.owner, 'unavailable_reason', lambda: 'fixture')
    cap = settings.capabilities()['secrets.set']
    assert cap['storage_only'] is True and cap['enrolls_backend'] is True
    assert cap['providers'] == ['anthropic', 'deepseek', 'glm', 'google', 'kimi', 'qwen']
    assert {p['id']: p['label'] for p in cap['provider_display']} == {
        'anthropic': 'Anthropic', 'deepseek': 'DeepSeek', 'google': 'Google',
        'glm': 'GLM (Z.ai)', 'kimi': 'Kimi (Moonshot)', 'qwen': 'Qwen (Alibaba Model Studio)'}


def test_fixed_projections_exclude_all_other_fields():
    value = dict(provider='deepseek', stored=True, fingerprint=fingerprint(CANARY), operation_id='safe', key=CANARY, stderr=CANARY)
    assert secret_result(value) == {k: value[k] for k in ('provider', 'stored', 'fingerprint', 'operation_id')}
    assert CANARY not in json.dumps(status_result({'phase': 'ready', 'operation_id': 'safe', 'result': value, 'stderr': CANARY}))
    with pytest.raises(SettingsError):
        secret_result({**value, 'stored': 1})
    with pytest.raises(SettingsError, match='invalid_args'):
        decode('{"key":"first","key":"second"}')


def test_policy_is_only_a_ceiling_on_signed_scope():
    base = str(Path('/project').resolve())
    policy = {'posture': 'scoped', 'scope': {'auto_actions': ['read', 'cc_edit'], 'may_touch': [base+'/**'], 'may_not_touch': [base+'/private/**']}}
    assert ceiling(policy, 'read', ['/project/file']) == 'auto'
    assert ceiling(policy, 'read', ['/project/private/key']) == 'human'
    assert ceiling(policy, 'shell') != 'auto'
    assert ceiling(policy, 'email_send') == 'human'
    assert evaluate_action('cc_edit', Scope(auto_actions=[])) != 'auto'
    assert ceiling({'posture': 'confirm', 'scope': {}}, 'read') == 'human'


@pytest.mark.asyncio
async def test_policy_ceiling_cannot_be_overruled_by_critic(monkeypatch):
    from core.nodes import gate
    from core.execution import policy
    from unittest.mock import AsyncMock
    monkeypatch.setattr(policy, 'gate_ceiling', AsyncMock(return_value='human'))
    critic = AsyncMock(return_value=('approve', ['irrelevant']))
    monkeypatch.setattr(gate, 'run_critic', critic)
    decision = await gate.gate_decide('cc_edit', {'file_path': '/project/file'},
                                      Scope(auto_actions=['cc_edit'], may_touch=['/project/**']), 'fixture')
    assert decision.destination == 'human'
    critic.assert_not_called()


def test_live_provider_key_read_and_legacy_default(monkeypatch):
    from core import settings_client
    from core.backends.deepseek import DeepSeekClient
    from core.backends.claude import ClaudeClient
    from core.backends.gemini import GeminiClient
    from core.backends.glm import GLMClient
    from core.backends.kimi import KimiClient
    from core.backends.qwen_cloud import QwenCloudClient
    value = ['first']
    hits = []
    def provider_key(provider, fallback):
        hits.append(provider)
        return value[0]
    monkeypatch.setattr(settings_client, 'provider_key', provider_key)
    clients = [DeepSeekClient(api_key='original'), ClaudeClient(api_key='original'), GeminiClient(api_key='original'),
               GLMClient(api_key='original'), KimiClient(api_key='original'), QwenCloudClient(api_key='original')]
    assert [client.api_key for client in clients] == ['first']*6
    value[0] = 'rotated'
    assert [client.api_key for client in clients] == ['rotated']*6
    assert hits == ['deepseek','anthropic','google','glm','kimi','qwen']*2
    assert GLMClient(api_key='aggregator', vault_provider=None).api_key == 'aggregator'


@pytest.mark.asyncio
async def test_http_owner_required_fixed_receipt_and_canary_failures(owner_h, monkeypatch, caplog):
    h = owner_h
    monkeypatch.setattr(settings.settings_client, 'unavailable_reason', lambda: None)
    app = web.Application()
    app.add_routes(settings.router)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = {'contract_version': 2, 'provider': 'deepseek', 'key': CANARY, 'request_key': 'once'}
        response = await client.post('/settings/secrets', json=body, headers=h.headers)
        assert response.status == 403
        owner, _ = await stepup(h.client, h.headers, 'secrets.set', {'provider': 'deepseek'})
        calls = []
        def stored(value):
            calls.append(value)
            return dict(provider='deepseek', stored=True, fingerprint=fingerprint(CANARY), operation_id='safe', key=CANARY)
        monkeypatch.setattr(settings.settings_client, 'call', stored)
        response = await client.post('/settings/secrets', json=body, headers=owner)
        assert response.status == 200
        assert await response.json() == dict(provider='deepseek', stored=True, fingerprint=fingerprint(CANARY), operation_id='safe')
        assert len(calls) == 1
        def failed(value):
            raise RuntimeError(CANARY)
        monkeypatch.setattr(settings.settings_client, 'call', failed)
        response = await client.post('/settings/secrets', json=body, headers=owner)
        assert response.status == 503 and CANARY not in await response.text()
        assert CANARY not in caplog.text
        for override in ('lane', 'path', 'unit', 'environment'):
            response = await client.post('/settings/secrets', json={**body, override: CANARY}, headers=owner)
            assert response.status == 422 and CANARY not in await response.text()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pre_settings_sessions_do_not_gain_new_operations(owner_h):
    from db import _sqlite_connection
    h = owner_h
    owner, _ = await stepup(h.client, h.headers)
    async with _sqlite_connection() as db:
        await db.execute('UPDATE owner_sessions SET allowed_operations=NULL')
        await db.commit()
    response = await h.client.get('/auth/owner/status', headers=owner)
    operations = (await response.json())['owner_session']['allowed_operations']
    assert 'runs.resume' in operations
    assert not {'secrets.set', 'policy.get', 'policy.set', 'flags.resolve'} & set(operations)
    from owner_sessions import authenticate
    from aiohttp.test_utils import make_mocked_request
    from core.execution.models import ExecutionError
    from core.execution.owner import require_owner
    from routers.execution import authority
    for operation, path in [('secrets.set', '/settings/secrets'), ('flags.resolve', '/agent-services/flags/flag-ref/resolve')]:
        request = make_mocked_request('POST', path, headers=owner)
        with pytest.raises(ExecutionError, match='owner_session_invalid'):
            await authenticate(request, operation)
        # Core independently checks the durable allowlist against a vouched claim.
        request['user'] = await authenticate(request, 'runs.resume')
        claims = dict(request['user'])
        claims['allowed_operations'] = [*claims['allowed_operations'], operation]
        request['user'] = claims
        with pytest.raises(ExecutionError, match='owner_session_invalid'):
            await require_owner(authority(request), operation)


def test_broker_canary_failures_and_exact_stored_projection(broker, monkeypatch):
    module, instance, _ = broker
    _, owner_calls, _, session = owner_transport(module, instance, monkeypatch)
    session['allowed_operations'] += ['secrets.set', 'policy.get', 'policy.set']
    instance.op_owner_confirm({'challenge_id': 'challenge', 'password': 'password-canary', 'totp': '123456'})
    body = {'contract_version': 2, 'provider': 'deepseek', 'key': CANARY, 'request_key': 'once'}
    calls = []
    def stored(*a, **k):
        calls.append(k)
        return dict(provider='deepseek', stored=True, fingerprint=fingerprint(CANARY), operation_id='safe', key=CANARY)
    instance._owner_transport.rest = stored
    result = instance.op_secrets_set(body)
    assert CANARY not in json.dumps(result) and len(calls) == 1
    def failed(*a, **k):
        calls.append(k)
        raise module.BrokerError(CANARY, CANARY)
    instance._owner_transport.rest = failed
    with pytest.raises(module.BrokerError) as caught:
        instance.op_secrets_set(body)
    assert CANARY not in str(caught.value) + caught.value.detail + caught.value.code
    assert len(calls) == 2 and CANARY not in module._OWNER_SECRETS
