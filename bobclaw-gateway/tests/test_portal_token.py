"""Portal bearer boundary, using real route templates without opening sockets."""
import json
import importlib.util
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import make_mocked_request

from auth import (_AGENT_ALLOWED_ROUTES, _PORTAL_ALLOWED_ROUTES, _LANE_SCOPE_ROOTS,
                  authenticate_ws, auth_middleware, create_agent_token,
                  create_portal_token, decode_access_token)
from config import config
# Router-template and auth tests need no POSIX file writes. On Windows load
# the unrelated work-memory router with a fail-closed flock stub; never pretend
# to test its Linux locking. Broker tests below still require real fcntl.
if sys.platform == 'win32':
    from types import ModuleType
    _fcntl = ModuleType('fcntl')
    _fcntl.LOCK_EX, _fcntl.LOCK_UN = 2, 8
    def _unsupported_flock(*args):
        raise NotImplementedError('POSIX file locking requires the Linux gate')
    _fcntl.flock = _unsupported_flock
    with patch.dict(sys.modules, {'fcntl': _fcntl}):
        from gateway import build_app
else:
    from gateway import build_app
from routers.auth_routes import mint_agent_token
from routers.chat import _agent_scope_fields, _face_denied_for_agent
from routers import agent_memory
from routers import conversations, approvals
from app_state import POSTGRES_POOL_KEY
from tests.fake_pool import InMemoryPostgresPool
from core.permissions import Scope, evaluate_path, scope_vouch


SCOPE = {'may_touch': ['/family/luna/**', '/family/shared/**'],
         'may_not_touch': ['/family/dad/**']}


@pytest.fixture(autouse=True)
def _isolated_gateway_db():
    # These boundary tests do not use DB handlers. Avoid the suite's SQLite
    # startup fixture so they also work in sandboxes that block loop wakeups.
    yield


async def request_for(method, canonical, token, *, app=None):
    if app is None:
        app = web.Application()
        app.router.add_route(method, canonical, AsyncMock())
    # Send concrete URLs, including new job/artifact parameters. Literal braces
    # are URL-encoded and resolve to a SystemRoute, not the allowed template.
    path = re.sub(r'\{[^{}]+\}', 'example', canonical)
    request = make_mocked_request(method, path, headers={'Authorization': 'Bearer ' + token}, app=app)
    request._match_info = await app.router.resolve(request)
    assert request.match_info.route.resource.canonical == canonical
    return request


def test_portal_claims_and_own_ttl(monkeypatch):
    monkeypatch.setattr(config, 'PORTAL_TOKEN_DAYS', 13)
    a = decode_access_token(create_portal_token('luna', SCOPE))
    b = decode_access_token(create_portal_token('luna', SCOPE))
    assert a['sub'] == 'luna' and a['token_type'] == 'portal'
    assert a['scope'] == SCOPE
    assert a['exp'] - a['iat'] == 13 * 86400
    assert a['jti'] != b['jti']
    assert 'faces' not in a


def test_all_portal_routes_are_actual_gateway_templates(monkeypatch, tmp_path):
    from routers import agent_services
    original_setup = agent_services.setup
    monkeypatch.setattr(agent_services, 'setup', lambda app: original_setup(app, root=tmp_path))
    routes = {(route.method, route.resource.canonical) for route in build_app().router.routes()}
    assert _PORTAL_ALLOWED_ROUTES <= routes


@pytest.mark.parametrize('method,canonical', sorted(_PORTAL_ALLOWED_ROUTES))
async def test_every_broker_rest_route_allowed(method, canonical):
    request = await request_for(method, canonical, create_portal_token('luna', SCOPE))
    handler = AsyncMock(return_value=web.json_response({'ok': True}))
    assert (await auth_middleware(request, handler)).status == 200
    handler.assert_awaited_once()


@pytest.mark.parametrize('mode', ['strict', 'open'])
@pytest.mark.parametrize('method,canonical', [
    ('GET', '/teams'), ('GET', '/faces/{face_id}'),
    ('DELETE', '/conversations/{conv_id}'), ('GET', '/approvals/digest'),
    ('GET', '/work-memory/status'), ('POST', '/work-memory/save'),
    ('GET', '/unknown'), ('HEAD', '/projects'),
])
async def test_portal_default_deny_even_in_open_mode(monkeypatch, mode, method, canonical):
    monkeypatch.setattr(config, 'AGENT_INNER_GATES', mode)
    request = await request_for(method, canonical, create_portal_token('luna', SCOPE))
    handler = AsyncMock()
    assert (await auth_middleware(request, handler)).status == 403
    handler.assert_not_awaited()


@pytest.mark.parametrize('mode', ['strict', 'open'])
async def test_work_scope_cannot_become_portal(monkeypatch, mode):
    monkeypatch.setattr(config, 'AGENT_INNER_GATES', mode)
    root = next(iter(_LANE_SCOPE_ROOTS.values()))
    token = create_portal_token('luna', {'may_touch': [root + '/**']})
    request = await request_for('GET', '/conversations', token)
    assert (await auth_middleware(request, AsyncMock())).status == 403


@pytest.mark.parametrize('mode,expected', [('strict', 403), ('open', 200)])
async def test_agent_existing_posture_unchanged(monkeypatch, mode, expected):
    monkeypatch.setattr(config, 'AGENT_INNER_GATES', mode)
    request = await request_for('GET', '/teams', create_agent_token('luna', scope=SCOPE))
    assert (await auth_middleware(request, AsyncMock(return_value=web.Response()))).status == expected


@pytest.mark.parametrize('mode', ['strict', 'open'])
@pytest.mark.parametrize('method,canonical', [
    ('POST', '/agent-memory/list'), ('GET', '/files/roots'),
    ('POST', '/files/list'), ('POST', '/files/read'),
])
async def test_owner_reads_never_admit_agent_or_admin(monkeypatch, mode, method, canonical):
    monkeypatch.setattr(config, 'AGENT_INNER_GATES', mode)
    from auth import create_access_token
    for token in (create_agent_token('luna', scope=SCOPE), create_access_token('admin')):
        request = await request_for(method, canonical, token)
        handler = AsyncMock()
        assert (await auth_middleware(request, handler)).status == 403
        handler.assert_not_awaited()
    assert (method, canonical) not in _AGENT_ALLOWED_ROUTES


@pytest.mark.parametrize('method,canonical', sorted(_AGENT_ALLOWED_ROUTES))
async def test_all_agent_routes_remain_allowed(method, canonical):
    request = await request_for(method, canonical, create_agent_token('luna', scope=SCOPE))
    assert (await auth_middleware(request, AsyncMock(return_value=web.Response()))).status == 200


async def test_portal_cannot_mint():
    request = await request_for('POST', '/auth/agent-token', create_portal_token('luna', SCOPE))
    assert (await mint_agent_token(request)).status == 403


@pytest.mark.parametrize('path,allowed', [('/ws/chat', True), ('/ws/approvals', True),
                                         ('/ws/monitor', False)])
@pytest.mark.parametrize('pattern', ['header', 'frame'])
async def test_portal_websockets(path, allowed, pattern):
    token = create_portal_token('luna', SCOPE)
    headers = {'Authorization': 'Bearer ' + token} if pattern == 'header' else {}
    request = make_mocked_request('GET', path, headers=headers)
    ws = SimpleNamespace(send_json=AsyncMock(), close=AsyncMock(), receive=AsyncMock(
        return_value=SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps({'type': 'auth', 'token': token}))))
    claims, initial = await authenticate_ws(request, ws)
    assert bool(claims) == allowed
    if allowed:
        assert claims['scope'] == SCOPE and claims['sub'] == 'luna'
        ws.close.assert_not_awaited()
    else:
        ws.close.assert_awaited_once()


async def test_portal_work_scope_cannot_chat():
    root = next(iter(_LANE_SCOPE_ROOTS.values()))
    token = create_portal_token('luna', {'may_touch': [root + '/**']})
    request = make_mocked_request('GET', '/ws/chat', headers={'Authorization': 'Bearer ' + token})
    ws = SimpleNamespace(send_json=AsyncMock(), close=AsyncMock())
    assert await authenticate_ws(request, ws, allow_agent=True) == (None, None)


@pytest.mark.parametrize('face', ['assistant', 'planner', 'planner-codex'])
def test_portal_scope_vouched_and_face_unrestricted(face, monkeypatch):
    monkeypatch.setattr(config, "AGENT_INNER_GATES", "strict")
    claims = decode_access_token(create_portal_token('luna', SCOPE))
    claims['faces'] = ['restricted-agent-face']
    assert not _face_denied_for_agent(claims, face)
    restricted = decode_access_token(create_agent_token('luna', scope=SCOPE, faces=['restricted-agent-face']))
    assert _face_denied_for_agent(restricted, face)
    assert _agent_scope_fields(claims, 'test-secret') == {
        'scope': SCOPE, 'scope_vouch': scope_vouch(SCOPE, 'test-secret')}
    assert evaluate_path('/family/dad/MEMORY.md', Scope.model_validate(SCOPE)) != 'auto'


def test_portal_memory_confined_to_lane_and_shared(tmp_path):
    app = web.Application()
    agent_memory.setup(app, root=tmp_path)
    request = make_mocked_request('GET', '/agent-memory/status', app=app)
    request['user'] = {'sub': 'luna-2', 'token_type': 'portal', 'scope': {
        'may_touch': [str(tmp_path / 'luna-2') + '/**', str(tmp_path / 'shared') + '/**']}}
    for owner in ('luna-2', 'dad', 'shared'):
        (tmp_path / owner).mkdir()
        (tmp_path / owner / 'MEMORY.md').write_text(owner + ' private evidence')
    assert {row['owner'] for row in agent_memory.sources(request)} == {'luna-2', 'shared'}
    assert not agent_memory.allowed(request, 'dad')


@pytest.mark.parametrize('kind', ['conversation', 'approval'])
async def test_portal_reads_only_owners_rows(kind):
    pool = InMemoryPostgresPool()
    app = web.Application()
    app[POSTGRES_POOL_KEY] = pool
    claims = decode_access_token(create_portal_token('luna', SCOPE))
    if kind == 'conversation':
        own = pool.add_conversation(title='Lane evidence', user_id='luna')
        other = pool.add_conversation(title='Other lane', user_id='dad')
        handler, path, parameter = conversations.get_conversation, '/conversations/', 'conv_id'
    else:
        own = pool.add_approval(user_id='luna')
        other = pool.add_approval(user_id='dad')
        handler, path, parameter = approvals.get_approval, '/approvals/', 'approval_id'
    for row, allowed in ((own, True), (other, False)):
        request = make_mocked_request('GET', path + row['id'], app=app,
                                      match_info={parameter: row['id']})
        request['user'] = claims
        if allowed:
            assert (await handler(request)).status == 200
        else:
            with pytest.raises(web.HTTPNotFound):
                await handler(request)


@pytest.fixture
def broker_module():
    pytest.importorskip('fcntl', reason='Broker locking is verified by the Linux gate')
    path = Path(__file__).resolve().parents[2] / 'portal-broker/portal-broker.py'
    spec = importlib.util.spec_from_file_location('_portal_broker_r6a_test', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        module.set_secret('')
        sys.modules.pop(spec.name, None)


def test_broker_accepts_portal_identity(broker_module):
    claims = decode_access_token(create_portal_token('luna', SCOPE))
    transport = SimpleNamespace(rest=lambda method, path: {'authenticated': True, 'user': 'luna'})
    identity = broker_module.bind_identity(transport, claims, 'http://127.0.0.1:7836')
    assert identity['sub'] == 'luna'
    assert identity['may_touch'] == SCOPE['may_touch']
    assert identity['faces'] == []


def test_broker_reloads_renewed_token_and_rejects_identity_change(broker_module, tmp_path):
    path = tmp_path / 'broker.token'
    first = create_portal_token('luna', SCOPE)
    second = create_portal_token('luna', SCOPE)
    path.write_text(first)
    path.chmod(0o600)
    transport = broker_module.LiveTransport('http://127.0.0.1:7836', first, token_file=str(path))
    assert transport._bearer() == first
    path.write_text(second)
    assert transport._bearer() == second
    path.write_text(create_portal_token('dad', SCOPE))
    with pytest.raises(broker_module.BrokerError, match='identity'):
        transport._bearer()


def test_broker_accepts_timer_migration_from_agent_to_portal(broker_module, tmp_path):
    path = tmp_path / 'broker.token'
    old = create_agent_token('luna', scope=SCOPE)
    new = create_portal_token('luna', SCOPE)
    path.write_text(new)
    path.chmod(0o600)
    transport = broker_module.LiveTransport('http://127.0.0.1:7836', old, token_file=str(path))
    assert transport._bearer() == new
