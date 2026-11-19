"""Canonical routes and owner-only decision floor through actual JWT middleware."""
import json
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
import pytest
from auth import auth_middleware,create_access_token,create_agent_token,create_portal_token
from config import config
from routers import tool_jobs
from core.execution.models import ExecutionError


@pytest.fixture
async def routes(monkeypatch):
    monkeypatch.setattr(config,'JWT_SECRET','tool-jobs-test-secret-with-32-characters')
    calls=[]
    async def proxy(req,op,args):
        calls.append((op,args));return web.json_response({'operation':op,'args':args})
    monkeypatch.setattr(tool_jobs,'proxy',proxy)
    app=web.Application(middlewares=[auth_middleware]);app.add_routes(tool_jobs.router)
    client=TestClient(TestServer(app));await client.start_server()
    try: yield client,calls
    finally: await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['strict','open'])
@pytest.mark.parametrize('kind',['human','agent','portal'])
@pytest.mark.parametrize('suffix,op',[('/audit','jobs.audit'),('/proposal','jobs.proposal'),('/artifacts/report-'+'a'*32+'.json','jobs.artifact')])
async def test_scoped_read_routes_bind_job_ids(routes,monkeypatch,mode,kind,suffix,op):
    client,calls=routes
    monkeypatch.setattr(config,'AGENT_INNER_GATES',mode)
    scope={'may_touch':['/family/owner/**']}
    token=(create_agent_token('owner',scope=scope) if kind=='agent' else
           create_portal_token('owner',scope) if kind=='portal' else create_access_token('owner'))
    response=await client.get('/api/tool-jobs/'+'a'*32+suffix,headers={'Authorization':'Bearer '+token})
    assert response.status==200 and calls[-1][0]==op and calls[-1][1]['job_id']=='a'*32
    if op=='jobs.artifact': assert calls[-1][1]['filename']=='report-'+'a'*32+'.json'


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['strict','open'])
@pytest.mark.parametrize('kind',['agent','portal'])
async def test_scoped_cancel_reaches_job_authorization(routes,monkeypatch,mode,kind):
    client,calls=routes
    monkeypatch.setattr(config,'AGENT_INNER_GATES',mode)
    scope={'may_touch':['/family/owner/**']}
    token=(create_agent_token('owner',scope=scope) if kind=='agent' else create_portal_token('owner',scope))
    response=await client.post('/api/tool-jobs/'+'a'*32+'/cancel',headers={'Authorization':'Bearer '+token},json={})
    assert response.status==200 and calls==[('jobs.cancel',{'job_id':'a'*32})]


@pytest.mark.asyncio
async def test_old_start_and_conflicting_request_key_never_forward(routes):
    client,calls=routes;headers={'Authorization':'Bearer '+create_access_token('owner')}
    response=await client.post('/api/tool-jobs',headers=headers,json={'role':'researcher','wall_seconds':120})
    assert response.status==409 and (await response.json())['error']['code']=='client_upgrade_required'
    response=await client.post('/api/tool-jobs',headers={**headers,'Idempotency-Key':'different'},
                               json={'contract_version':2,'request_key':'original'})
    assert response.status==422 and calls==[]


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['strict','open'])
async def test_plain_human_agent_and_portal_cannot_decide(routes,monkeypatch,mode):
    client,calls=routes
    monkeypatch.setattr(config,'AGENT_INNER_GATES',mode)
    for token in [create_access_token('owner'),create_agent_token('owner',scope={'may_touch':['*']}),
                  create_portal_token('owner',{'may_touch':['*']})]:
        response=await client.post('/api/tool-jobs/'+'a'*32+'/proposal/decision',headers={'Authorization':'Bearer '+token},
                                  json={'contract_version':2,'decision':'approve'})
        assert response.status in {403,404} and calls==[]


from tests.test_owner_sessions import owner_h,stepup


@pytest.mark.asyncio
async def test_jobs_decision_uses_real_owner_session_vouch_and_no_legacy_widening(owner_h):
    from core.execution.models import Authority
    from core.execution.owner import require_owner
    from db import _sqlite_connection
    h=owner_h;identifier='a'*32
    owner,result=await stepup(h.client,h.headers,'jobs.decide',{'job_id':identifier})
    assert h.hits[-1]['envelope']['operation']=='jobs.proposal'
    assert h.hits[-1]['envelope']['args']=={'job_id':identifier}
    args={'contract_version':2,'decision':'approve','proposal_digest':'b'*64,'expected_revision':3,'request_key':'decision-key'}
    path='/api/tool-jobs/'+identifier+'/proposal/decision'
    response=await h.client.post(path,headers=owner,json=args)
    assert response.status==200
    envelope=h.hits[-1]['envelope']
    assert envelope['operation']=='jobs.decide'
    assert envelope['args']=={k:v for k,v in args.items() if k!='contract_version'} | {'job_id':identifier}
    await require_owner(Authority(**envelope['authority']),'jobs.decide')
    assert 'jobs.decide' in result['owner_session']['allowed_operations']
    assert (await h.client.get('/api/tool-jobs/'+identifier+'/proposal',headers=owner)).status==403
    async with _sqlite_connection() as db:
        await db.execute('UPDATE owner_sessions SET allowed_operations=NULL');await db.commit()
    assert (await h.client.post(path,headers=owner,json=args)).status==403
