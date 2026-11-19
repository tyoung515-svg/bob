"""Real route resolution, JWT and middleware before the PC router's dispatch."""
from unittest.mock import AsyncMock
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import auth
from config import config
from routers import pc


async def request(operation, token, body):
    app = web.Application()
    app.add_routes(pc.router)
    req = make_mocked_request('POST', '/api/pc/' + operation,
        headers={'Authorization': 'Bearer ' + token}, app=app)
    req._match_info = await app.router.resolve(req)
    assert req.match_info.route.resource.canonical == '/api/pc/{operation}'
    req.json = AsyncMock(return_value=body)
    return req


@pytest.mark.parametrize('mode', ['strict', 'open'])
@pytest.mark.parametrize('kind', ['portal', 'human', 'agent'])
async def test_native_pc_transport_passes_real_middleware_only_for_portal(monkeypatch, mode, kind):
    monkeypatch.setattr(config, 'AGENT_INNER_GATES', mode)
    scope = {'may_touch': ['/family/luna/**']}
    token = (auth.create_portal_token('luna', scope) if kind == 'portal' else
             auth.create_agent_token('luna', scope=scope) if kind == 'agent' else
             auth.create_access_token('luna'))
    proxy = AsyncMock(return_value=web.json_response({'requests': []}))
    monkeypatch.setattr(pc, 'proxy', proxy)
    req = await request('pending', token, {'contract_version': 2})
    response = await auth.auth_middleware(req, pc.command)
    assert response.status == (200 if kind == 'portal' else 403)
    if kind == 'portal':
        proxy.assert_awaited_once_with(req, 'pc.pending', {})
    else:
        proxy.assert_not_awaited()


async def test_native_transport_does_not_create_an_approval_route(monkeypatch):
    proxy = AsyncMock()
    monkeypatch.setattr(pc, 'proxy', proxy)
    req = await request('approve', auth.create_portal_token('luna', {'may_touch': ['/family/luna/**']}),
                        {'contract_version': 2, 'decision': 'allow'})
    assert (await auth.auth_middleware(req, pc.command)).status == 403
    proxy.assert_not_awaited()


@pytest.mark.parametrize('body', [{}, {'contract_version': True}, {'contract_version': 1}, []])
async def test_native_transport_requires_explicit_v2_after_middleware(monkeypatch, body):
    proxy = AsyncMock()
    monkeypatch.setattr(pc, 'proxy', proxy)
    req = await request('pending', auth.create_portal_token('luna', {'may_touch': ['/family/luna/**']}), body)
    assert (await auth.auth_middleware(req, pc.command)).status == 422
    proxy.assert_not_awaited()
