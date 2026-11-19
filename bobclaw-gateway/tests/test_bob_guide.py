"""GL18: authenticated gateway scope is the guide disclosure boundary."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
import pytest
from core.faces.registry import FaceRegistry
from core.guide import render
from routers import faces


class Request(dict):
    def __init__(self, claims, **query):
        super().__init__(user=claims)
        self.query = query


@pytest.fixture
def guide_gateway(monkeypatch):
    registry = FaceRegistry(Path(__file__).resolve().parents[2] / 'bobclaw-core/core/faces/templates')
    roster = [s.model_dump() for s in registry.list_faces()]
    monkeypatch.setattr(faces, '_proxy_json', AsyncMock(return_value=roster))
    hits = []
    class Response:
        status = 200
        def __init__(self, body): self.body = body
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def json(self):
            return await render(self.body['topic'], registry=registry,
                                face_id=self.body['face_id'], visible_faces=self.body['visible_faces'], backend_rows=[])
    class Session:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def post(self, url, *, json):
            hits.append((url, json))
            return Response(json)
    monkeypatch.setattr(faces.aiohttp, 'ClientSession', Session)
    return hits


@pytest.mark.asyncio
@pytest.mark.parametrize('topic', ['agents', 'tools', 'models', 'limits'])
async def test_guide_uses_verified_claims_not_client_visibility(guide_gateway, topic):
    request = Request({'token_type': 'agent', 'sub': 'owner', 'faces': ['reviewer']},
                      topic=topic, visible_faces='assistant,builder')
    response = await faces.bob_guide(request)
    payload = json.loads(response.text)
    assert response.status == 200 and payload['face'] == 'reviewer'
    assert guide_gateway[0][1]['visible_faces'] == ['reviewer']
    assert not any(line.startswith(('assistant:', 'builder:', 'fixer:', 'orchestrator:'))
                   for line in payload['content'].splitlines())
    assert 'fs_write:' not in payload['content'] and 'pc_run:' not in payload['content']


@pytest.mark.asyncio
async def test_guide_rejects_forbidden_face_before_core_call(guide_gateway):
    with pytest.raises(web.HTTPForbidden):
        await faces.bob_guide(Request({'token_type': 'agent', 'faces': ['reviewer']}, face_id='builder'))
    assert guide_gateway == []


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [False, 'reviewer', {'reviewer': True}, ['reviewer', 1], ['reviewer'] * 33])
async def test_malformed_scope_never_becomes_unrestricted(guide_gateway, invalid):
    with pytest.raises(web.HTTPForbidden):
        await faces.bob_guide(Request({'token_type': 'agent', 'faces': invalid}))
    assert guide_gateway == []


def test_read_route_is_explicit_and_work_lane_stays_confined():
    import auth
    assert ('GET', '/bob-guide') in auth._AGENT_ALLOWED_ROUTES
    assert ('GET', '/bob-guide') in auth._PORTAL_ALLOWED_ROUTES
    assert ('POST', '/bob-guide') not in auth._AGENT_ALLOWED_ROUTES
    assert ('GET', '/bob-guide') not in auth._LANE_ALLOWED_ROUTES
