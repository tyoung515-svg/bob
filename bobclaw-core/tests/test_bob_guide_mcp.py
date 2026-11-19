"""Real FastMCP registration and authenticated guide proxy; no model calls."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from core.mcp.server import MCPConfig, build_server


@pytest.fixture(autouse=True)
def mock_redis():
    yield


@pytest.mark.asyncio
async def test_mcp_guide_uses_gateway_auth_scope_and_resource(monkeypatch):
    hits = []
    class Response:
        status = 200
        async def text(self):
            return json.dumps({'topic': 'tools', 'face': 'reviewer', 'content': 'bob_guide: Read guide'})
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, method, url, **kwargs):
            hits.append((method, url, kwargs))
            return Response()
    monkeypatch.setattr('core.mcp.server.aiohttp.ClientSession', Session)
    mcp = build_server(MCPConfig('scope-canary', 'http://gateway', ['reviewer']))
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    assert 'bob_guide' in tools
    denied = await tools['bob_guide'].fn('tools', 'builder')
    assert 'forbidden' in denied and hits == []
    out = json.loads(await tools['bob_guide'].fn('tools', 'reviewer'))
    assert out['face'] == 'reviewer'
    assert hits[0][0:2] == ('GET', 'http://gateway/bob-guide')
    assert hits[0][2]['headers'] == {'Authorization': 'Bearer scope-canary'}
    assert hits[0][2]['params'] == {'topic': 'tools', 'face_id': 'reviewer'}
    templates = await mcp.list_resource_templates()
    assert any(str(t.uriTemplate) == 'bob://guide/{topic}' for t in templates)
    resource = await mcp.read_resource('bob://guide/tools')
    assert 'bob_guide' in str(resource)


@pytest.mark.asyncio
async def test_gateway_guide_denial_is_not_silently_replaced(monkeypatch):
    class Response:
        status = 403
        async def text(self): return 'outside scope'
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def request(self, *args, **kwargs): return Response()
    monkeypatch.setattr('core.mcp.server.aiohttp.ClientSession', Session)
    mcp = build_server(MCPConfig('token', 'http://gateway', []))
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == 'bob_guide')
    assert (await tool.fn()) == '[error:http_403] outside scope'
