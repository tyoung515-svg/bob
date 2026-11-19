"""Builder owner controls through real middleware and durable SQLite sessions."""
import json
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from auth import auth_middleware, create_agent_token
from core.execution.models import Authority
from core.execution.owner import require_owner, WORK_APPROVAL_OPERATIONS, session_operations
from tests.test_owner_sessions import owner_h, stepup


@pytest.fixture
def tmp_path(tmp_path_factory):
    import os
    import uuid
    from pathlib import Path
    if os.name != 'nt':return tmp_path_factory.mktemp('builder-owner')
    path=Path(__file__).parents[1]/'test-db'/('builder-'+uuid.uuid4().hex)
    path.mkdir(mode=0o777)
    return path


@pytest.fixture
async def builder_h(owner_h):
    from owner_sessions import router as owners
    from routers.builder import router
    app=web.Application(middlewares=[auth_middleware])
    app.add_routes(owners);app.add_routes(router)
    client=TestClient(TestServer(app));await client.start_server()
    try:
        yield owner_h,client
    finally:
        await client.close()


@pytest.mark.parametrize('operation,resource,args',[
    ('builder.mode.set',{'conversation_id':'owned-conversation'},
        {'conversation_id':'owned-conversation','mode':'auto','expected_revision':1,'request_key':'mode'}),
    ('builder.approve',{'approval_id':'a'*32},
        {'approval_id':'a'*32,'decision':'allow_once','request_key':'approval'})])
async def test_exact_owner_route_preserves_authority_and_blocks_portal_mutation(builder_h,operation,resource,args):
    h,client=builder_h
    headers,_=await stepup(client,h.headers,operation,resource)
    path='/api/builder/'+operation.removeprefix('builder.')
    response=await client.post(path,headers=headers,json={'contract_version':2,**args})
    assert response.status==200,await response.text()
    envelope=h.hits[-1]['envelope']
    assert envelope['operation']==operation and envelope['args']==args
    await require_owner(Authority(**envelope['authority']),operation)
    response=await client.post(path,headers=h.headers,json={'contract_version':2,**args})
    assert response.status==403
    response=await client.post('/api/builder/mode.get',headers=headers,json={'contract_version':2,'conversation_id':'x'})
    assert response.status==403


async def test_portal_reads_and_preview_use_general_authority(builder_h):
    h,client=builder_h
    for operation,args in [('mode.get',{'conversation_id':'owned'}),('preview',{'conversation_id':'owned','path':'index.html'}),
                           ('approval.get',{'approval_id':'a'*32})]:
        response=await client.post('/api/builder/'+operation,headers=h.headers,json={'contract_version':2,**args})
        assert response.status==200,await response.text()
        envelope=h.hits[-1]['envelope']
        assert envelope['operation']=='builder.'+operation and envelope['args']==args
        assert envelope['authority']['token_type']=='portal' and not envelope['authority']['session_kind']
    response=await client.post('/api/builder/arbitrary',headers=h.headers,json={'contract_version':2})
    assert response.status==404


async def test_signed_in_confirmation_includes_builder_operations(builder_h,monkeypatch):
    h,client=builder_h
    monkeypatch.setenv('BOB_OWNER_WORK_APPROVAL','signed_in_user')
    assert {'builder.mode.set','builder.approve'} <= WORK_APPROVAL_OPERATIONS
    response=await client.post('/auth/owner/begin',headers=h.headers,json={
        'operation':'builder.mode.set','resource':{'conversation_id':'owned'}})
    assert response.status==200,await response.text()
    challenge=await response.json()
    assert challenge['confirmation_mode']=='signed_in_user'
    response=await client.post('/auth/owner/confirm',headers=h.headers,json={
        'challenge_id':challenge['challenge_id'],'confirmation_mode':'signed_in_user'})
    assert response.status==200,await response.text()


def test_legacy_sessions_do_not_gain_new_permissions():
    assert not {'builder.mode.set','builder.approve'} & session_operations({'allowed_operations':None})


@pytest.mark.parametrize('operation,args,owner',[
    ('mode.get',{'conversation_id':'owned'},False),
    ('mode.set',{'conversation_id':'owned','mode':'yolo','expected_revision':1,'request_key':'mode','yolo_acknowledged':True},True),
    ('approve',{'approval_id':'approval','decision':'allow_once','request_key':'allow'},True),
    ('preview',{'conversation_id':'owned','path':'index.html'},False),
    ('approval.get',{'approval_id':'approval'},False)])
def test_broker_builder_routes_exact_owner_transport(monkeypatch,operation,args,owner):
    from pathlib import Path
    from tests.test_day_one_broker_sync import load_broker
    from types import SimpleNamespace
    module=load_broker(Path(__file__).parents[2]/'portal-broker/portal-broker.py',monkeypatch)
    broker=module.Broker(SimpleNamespace(),{'sub':'owner','token_type':'portal'},SimpleNamespace())
    hits=[]
    broker._require_owner_operation=lambda op:hits.append(('capability',op))
    broker._rest=lambda method,path,**kwargs:hits.append(('portal',method,path,kwargs)) or {'ok':True}
    broker._owner_rest=lambda method,path,**kwargs:hits.append(('owner',method,path,kwargs)) or {'ok':True}
    assert broker.op_builder(operation,args)=={'ok':True}
    assert hits[0]==('capability','builder.'+operation)
    assert hits[1][:3]==('owner' if owner else 'portal','POST','/api/builder/'+operation)
    assert hits[1][3]['body']=={**args,'contract_version':2}
