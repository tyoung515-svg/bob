"""Verified gateway face/owner/scope forwarding without model calls or sessions."""
import json
from unittest.mock import AsyncMock
import pytest
from routers import inspection, faces
from config import config
from core.inspection_api import valid


class Request(dict):
    def __init__(self,claims,operation='project_read',body=None):
        super().__init__(user=claims)
        self.match_info={'operation':operation}
        self.body=body if body is not None else {'project_id':'project'}
    async def json(self):return self.body


@pytest.fixture
def gateway(monkeypatch):
    monkeypatch.setattr(config,'BOBCLAW_SECRET','bu10-test-signing-fixture')
    monkeypatch.setattr('core.execution.owner.owner_mapping',lambda owner:owner=='owner')
    monkeypatch.setattr('core.execution.owner.current_epoch',AsyncMock(return_value=7))
    monkeypatch.setattr(faces,'_proxy_json',AsyncMock(return_value=[{'id':'assistant'},{'id':'planner'},{'id':'reviewer'}]))
    hits=[]
    class Response:
        status=200
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def read(self):return b'{"read":true}'
    class Session:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        def post(self,url,*,json):
            hits.append((url,json))
            return Response()
    monkeypatch.setattr(inspection.aiohttp,'ClientSession',Session)
    return hits


@pytest.mark.parametrize('allowed',[['assistant'],['planner'],[]])
async def test_proxy_preserves_verified_owner_scope_and_face_allowlist(gateway,allowed):
    claims={'sub':'owner','token_type':'agent','agent_id':'agent','faces':allowed,'scope':{'may_touch':['project/*']}}
    response=await inspection.inspect(Request(claims))
    assert response.status==200
    url,body=gateway[0]
    envelope=body['envelope']
    assert valid(envelope,body['vouch'],config.BOBCLAW_SECRET)
    assert envelope['authority']['owner']=='owner' and envelope['authority']['actor']=='agent'
    assert envelope['authority']['scope']==claims['scope'] and envelope['authority']['credential_epoch']==7
    assert envelope['faces']==(allowed or ['assistant','planner','reviewer'])
    assert envelope['args']=={'project_id':'project'}
    assert url.endswith('/api/inspection/project_read')


@pytest.mark.parametrize('allowed',[['reviewer'],False,'',{},['assistant',1],['assistant']*33])
async def test_face_scope_is_strict_even_in_open_mode(gateway,allowed):
    response=await inspection.inspect(Request({'sub':'owner','token_type':'agent','faces':allowed}))
    assert response.status==403 and gateway==[]


@pytest.mark.parametrize('operation',['fs_write','workflows.grant_start','project_files_write','pc.run'])
async def test_read_route_cannot_name_a_mutation(gateway,operation):
    response=await inspection.inspect(Request({'sub':'owner','faces':['assistant']},operation))
    assert response.status==403 and gateway==[]


async def test_foreign_owner_and_non_object_body_are_refused(gateway):
    assert (await inspection.inspect(Request({'sub':'other','faces':['assistant']}))).status==403
    assert (await inspection.inspect(Request({'sub':'owner','faces':['assistant']},body=[]))).status==422
    assert gateway==[]


def test_route_is_explicit_but_does_not_admit_other_lane_tokens():
    import auth
    route=('POST','/bob-inspection/{operation}')
    assert route in auth._AGENT_ALLOWED_ROUTES and route in auth._PORTAL_ALLOWED_ROUTES
    assert route not in auth._LANE_ALLOWED_ROUTES
