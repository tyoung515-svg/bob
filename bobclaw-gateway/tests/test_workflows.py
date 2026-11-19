"""Workflow route/vouch and broker smoke tests. No production core or models."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import auth_middleware, create_agent_token, create_portal_token
from config import config
import owner_sessions  # Bind its original imports before per-test monkeypatches.
from core.permissions import verify_execution_vouch
from routers import workflows
from tests.test_day_one_broker_sync import BACKEND, load_broker


@pytest.fixture(autouse=True)
def _isolated_gateway_db():
    yield  # Minimal HTTP apps and broker tests do not open gateway SQLite.


@pytest.fixture
async def client(monkeypatch):
    hits=[]
    monkeypatch.setattr(config,'BOBCLAW_SECRET','workflow-test-vouch-secret')
    monkeypatch.setattr('core.execution.owner.owner_mapping',lambda owner:False)
    async def command(request):
        value=await request.json(); envelope=value['envelope']
        assert verify_execution_vouch(envelope,value['vouch'],config.BOBCLAW_SECRET)
        hits.append(envelope)
        return web.json_response({'received':envelope['args']})
    core=web.Application(); core.router.add_post('/api/execution/{op}',command)
    core_server=TestServer(core); await core_server.start_server()
    monkeypatch.setattr(config,'CORE_URL',str(core_server.make_url('/')).rstrip('/'))
    app=web.Application(middlewares=[auth_middleware]); app.add_routes(workflows.router); app.add_routes(owner_sessions.router)
    client=TestClient(TestServer(app)); await client.start_server()
    try: yield client,hits
    finally:
        await client.close(); await core_server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('method,path,op',workflows.ROUTES)
async def test_every_gateway_route(client,monkeypatch,method,path,op):
    c,hits=client
    path=path.replace('{id}','definition-1').replace('{proposal_id}','proposal-1').replace('{run_id}','run-1').replace('{wait_id}','wait-1').replace('{artifact_id}','artifact-1')
    if op in {'grant_start','resume','gate'}:
        authenticate=AsyncMock(return_value={'sub':'owner','token_type':'human','session_kind':'owner_stepup'})
        monkeypatch.setattr('owner_sessions.authenticate',authenticate)
    body={'contract_version':1}
    headers={'Authorization':'Bearer '+create_portal_token('owner',{})}
    response=await c.request(method,path,headers=headers,**({'params':body} if method=='GET' else {'json':body}))
    assert response.status==200
    assert hits[-1]['operation']=='workflows.'+op
    assert hits[-1]['args']['contract_version']==1
    if '/run-1' in path: assert hits[-1]['args']['run_id']=='run-1'
    if op in {'grant_start','resume','gate'}: assert authenticate.await_count>=1


@pytest.mark.asyncio
async def test_route_refuses_unknown_and_path_override(client):
    c,hits=client; headers={'Authorization':'Bearer '+create_portal_token('owner',{})}
    for path,body in [('/workflows',{'contract_version':1,'unknown':1}),
                      ('/workflow-runs/run-1/stop',{'contract_version':1,'run_id':'run-2'}),
                      ('/workflow-proposals',{'contract_version':2})]:
        assert (await c.post(path,headers=headers,json=body)).status==422
    assert not hits


@pytest.mark.asyncio
async def test_agent_cannot_reach_owner_click_transport(client,monkeypatch):
    c,hits=client
    from core.execution.models import ExecutionError
    monkeypatch.setattr('owner_sessions.authenticate',AsyncMock(side_effect=ExecutionError('not_found',404)))
    headers={'Authorization':'Bearer '+create_agent_token('owner',faces=['researcher'])}
    for path in ('/workflow-proposals/p/start','/workflow-runs/r/resume','/workflow-runs/r/gates/g'):
        response=await c.post(path,headers=headers,json={'contract_version':1,'request_key':'denied'})
        assert response.status in {403,404}
    assert not hits


@pytest.mark.asyncio
async def test_real_signed_in_user_owner_session_workflow_click(client,monkeypatch):
    import uuid
    from db import init_db
    c,hits=client
    path=BACKEND/'bobclaw-gateway/test-db'/('wb1-owner-'+uuid.uuid4().hex+'.db')
    monkeypatch.setenv('GATEWAY_SQLITE_PATH',str(path))
    monkeypatch.setenv('BOB_DEFAULT_OWNER','owner')
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY','worker')
    monkeypatch.setenv('BOB_OWNER_WORK_APPROVAL','signed_in_user')
    monkeypatch.setattr('core.execution.owner.owner_mapping',owner_sessions.owner_mapping)
    await init_db()
    token=create_portal_token('owner',{})
    portal={'Authorization':'Bearer '+token,'X-Bob-Broker-Link':'workflow-owner-link'}
    response=await c.post('/auth/owner/begin',headers=portal,json={'contract_version':2,'operation':'workflows.grant_start',
        'resource':{'proposal_id':'proposal-1'}})
    assert response.status==200,await response.text()
    challenge=await response.json()
    response=await c.post('/auth/owner/confirm',headers=portal,json={'contract_version':2,'challenge_id':challenge['challenge_id'],
        'confirmation_mode':'signed_in_user'})
    assert response.status==200,await response.text()
    session=await response.json()
    assert 'workflows.grant_start' in session['owner_session']['allowed_operations']
    import jwt
    headers={'Authorization':'Bearer '+session['owner_bearer'],'X-Bob-Broker-Link':'workflow-owner-link',
             'X-Bob-Portal-Credential':jwt.decode(token,options={'verify_signature':False})['jti']}
    response=await c.post('/workflow-proposals/proposal-1/start',headers=headers,json={
        'contract_version':1,'proposal_revision':1,'proposal_digest':'0'*64,'request_key':'same-click'})
    assert response.status==200,await response.text()
    assert hits[-1]['operation']=='workflows.grant_start'
    assert hits[-1]['authority']['session_kind']=='owner_stepup'
    assert hits[-1]['authority']['actor']==hits[-1]['authority']['owner']=='owner'


@pytest.fixture
def broker(monkeypatch):
    module=load_broker(BACKEND/'portal-broker/portal-broker.py',monkeypatch)
    sink=SimpleNamespace(event=Mock())
    transport=SimpleNamespace(rest=Mock(return_value={'fixture':True}))
    b=module.Broker(transport,{'sub':'owner'},sink)
    b._rest=Mock(return_value={'fixture':True}); b._owner_rest=Mock(return_value={'fixture':True})
    return module,b,sink


@pytest.mark.parametrize('method,path,op',workflows.ROUTES)
def test_every_broker_method(broker,method,path,op):
    module,b,_=broker
    args={'contract_version':1}
    for key in ('id','proposal_id','run_id','wait_id','artifact_id'):
        if '{'+key+'}' in path: args[key]=key+'-1'
    result=b._handlers['workflows.'+op](args)
    rest=b._owner_rest if op in {'grant_start','resume','gate'} else b._rest
    expected=path
    for key,value in args.items(): expected=expected.replace('{'+key+'}',str(value))
    rest.assert_called_once()
    assert rest.call_args.args==(method,expected)
    assert rest.call_args.kwargs['params' if method=='GET' else 'body']=={'contract_version':1}
    assert result=={'fixture':True}


def test_broker_strict_fields_and_version(broker):
    module,b,_=broker
    for args in ({'contract_version':2},{'contract_version':True},{'contract_version':1,'unknown':1}):
        with pytest.raises(module.BrokerError): b.op_workflows('catalog',args)
    b._rest.assert_not_called()


@pytest.mark.asyncio
async def test_expired_history_and_again_gateway_wire(client):
    c,hits=client
    headers={'Authorization':'Bearer '+create_portal_token('owner',{})}
    response=await c.get('/workflow-proposals',headers=headers,
        params={'contract_version':'1','include_expired':'true','cursor':'opaque','limit':'2'})
    assert response.status==200
    assert hits[-1]['args']=={'contract_version':1,'include_expired':True,'cursor':'opaque','limit':'2'}
    response=await c.post('/workflow-proposals/old/propose-again',headers=headers,json={'contract_version':1})
    assert response.status==200
    assert hits[-1]['operation']=='workflows.propose_again'
    assert hits[-1]['args']=={'contract_version':1,'proposal_id':'old'}
    previous=len(hits)
    for value in ('True','1','yes',''):
        assert (await c.get('/workflow-proposals',headers=headers,
            params={'contract_version':'1','include_expired':value})).status==422
    assert (await c.post('/workflow-proposals/old/propose-again',headers=headers,
        json={'contract_version':1,'proposal_id':'override'})).status==422
    assert (await c.post('/workflow-proposals/old/propose-again',headers=headers,
        json={'contract_version':1,'request_key':'not-on-this-wire'})).status==422
    assert len(hits)==previous


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['strict','open'])
async def test_agent_cannot_forward_again_to_core(client,monkeypatch,mode):
    c,hits=client
    monkeypatch.setattr(config,'AGENT_INNER_GATES',mode)
    headers={'Authorization':'Bearer '+create_agent_token('owner',faces=['researcher'])}
    response=await c.post('/workflow-proposals/old/propose-again',headers=headers,json={'contract_version':1})
    assert response.status==403
    # Strict middleware refuses this route before the handler; open mode still
    # reaches the handler's explicit owner-only refusal.
    payload=await response.json()
    assert payload.get('error')
    if mode=='open':
        assert payload['error']['code']=='proposal_again_not_permitted'
    assert not hits


def test_broker_expired_history_and_again_preserve_proposal_shape(broker):
    module,b,_=broker
    proposal={'id':'fresh','state':'proposed','created_at':'2026-11-17T00:00:00Z',
        'origin_conversation_id':'conversation','proposed_by':{'actor':'owner','token_type':'portal','via':'owner'},
        'previous_proposal_id':'old','definition_changed':True,
        'definition_change_notice':'The workflow definition has changed. This proposal uses the current definition.'}
    envelope={'items':[{'kind':'workflow_proposal',**proposal}],'next_cursor':'opaque'}
    b._rest.return_value=envelope
    assert b._handlers['workflows.proposals']({'contract_version':1,'include_expired':True,
        'cursor':'previous','limit':2})==envelope
    assert b._rest.call_args.args==('GET','/workflow-proposals')
    assert b._rest.call_args.kwargs['params']=={'contract_version':1,'include_expired':'true','cursor':'previous','limit':2}
    b._rest.reset_mock(); b._rest.return_value={'proposal':proposal}
    assert b._handlers['workflows.propose_again']({'contract_version':1,'proposal_id':'old'})=={'proposal':proposal}
    assert b._rest.call_args.args==('POST','/workflow-proposals/old/propose-again')
    assert b._rest.call_args.kwargs['body']=={'contract_version':1}
    b._owner_rest.assert_not_called()
    for value in ('true',1,None):
        with pytest.raises(module.BrokerError):
            b._handlers['workflows.proposals']({'contract_version':1,'include_expired':value})
    with pytest.raises(module.BrokerError):
        b._handlers['workflows.propose_again']({'contract_version':1,'proposal_id':'old','request_key':'extra'})


def test_broker_hello_advertises_expired_history_and_again(broker):
    from core.workflow_surface import capabilities
    _,b,_=broker
    operations=capabilities(True,True)
    b._rest.return_value={'operations':operations}
    result=b.op_hello({})
    assert result['operations']['workflows.proposals']==operations['workflows.proposals']
    assert result['operations']['workflows.propose_again']==operations['workflows.propose_again']


def test_watch_replay_tags_and_disconnect_does_not_stop(broker):
    module,b,sink=broker
    seq=9007199254740993
    event={'run_id':'run-1','seq':str(seq),'revision':str(seq),'at':'2026-11-16T00:00:00Z','kind':'chat',
           'stage_key':'work','conversation_id':'conversation-1','request_id':'request-1',
           'payload':{'name':'chunk','data':{'content':'Built','model':'fixture','backend':'fixture'}}}
    b.op_workflows=Mock(side_effect=[{'events':[event,event],'next_seq':str(seq),'has_more':True},module.BrokerError('offline')])
    stop=threading.Event(); b._workflow_watches['run-1']=stop
    b._watch_workflow('run-1',seq-1,stop)
    assert [x.args[0] for x in sink.event.call_args_list]==['workflow_event','chunk','workflow_connection']
    data=sink.event.call_args_list[1].args[1]
    assert data['event_seq']==str(seq) and data['request_id']=='request-1' and data['conversation_id']=='conversation-1'
    assert data['source']=='workflow' and data['stage_key']=='work'
    assert all(x.args[0]=='events' for x in b.op_workflows.call_args_list)


def test_unwatch_has_no_run_mutation(broker):
    _,b,_=broker; stop=threading.Event(); b._workflow_watches['r']=stop
    assert b.op_workflow_watch({'contract_version':1,'run_id':'r','after_seq':0,'watch':False})=={'run_id':'r','watching':False}
    assert stop.is_set(); b._rest.assert_not_called(); b._owner_rest.assert_not_called()
