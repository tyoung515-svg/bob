import asyncio
import json
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient,TestServer
from auth import auth_middleware,create_agent_token
from routers import agent_services as svc

@pytest.fixture
async def client(tmp_path,monkeypatch):
 from unittest.mock import AsyncMock
 monkeypatch.setattr(svc,'local_status',AsyncMock(return_value={'available':False}))
 app=web.Application(middlewares=[auth_middleware]); app.router.add_routes(svc.router)
 svc.setup(app,root=tmp_path)
 c=TestClient(TestServer(app));await c.start_server();yield c;await c.close()

def headers(faces=None,sub='admin'):
 return {'Authorization':'Bearer '+create_agent_token(sub,faces=faces if faces is not None else ['researcher'])}

@pytest.mark.asyncio
async def test_catalog_auth_and_status(client):
 assert (await client.get('/agent-services/catalog')).status==401
 r=await client.get('/agent-services/catalog',headers=headers());assert r.status==200
 d=await r.json();assert len(d['model_faces'])==18;assert len(d['teams'])==2
 assert d['openrouter']['enabled'] is False
 assert next(t for t in d['teams'] if t['id']=='coding')['runnable'] is False

@pytest.mark.asyncio
async def test_face_permission_and_unsupported_tools(client):
 r=await client.post('/v1/chat/completions',headers=headers(['assistant']),json={'model':'bob-kimi-k3','messages':[{'role':'user','content':'Hi'}]});assert r.status==403
 r=await client.post('/v1/chat/completions',headers=headers(),json={'model':'bob-kimi-k3','tools':[{'type':'function'}],'messages':[{'role':'user','content':'Hi'}]});assert r.status==422

@pytest.mark.asyncio
async def test_blocked_seats_and_budgets(client):
 for body in [{'team':'coding','task':'edit'}, {'team':'balance-budget','task':'audit','require_bulk':True}, {'team':'balance-budget','task':'audit','max_model_calls':99}, {'team':'balance-budget','task':'audit','budget_usd':1}]:
  r=await client.post('/agent-services/runs',headers=headers(),json=body);assert r.status in (409,422)

@pytest.mark.asyncio
async def test_provider_conversation_and_actual_usage(client,monkeypatch):
 async def fake_turn(request,seat,text,conversation_id=None,project_id=None):
  return {'conversation_id':conversation_id or 'fixture-conversation','text':'fixture answer','usage':{'prompt_tokens':3,'completion_tokens':2},'events':[]}
 monkeypatch.setattr(svc,'turn',fake_turn)
 r=await client.post('/v1/chat/completions',headers=headers(),json={'model':'bob-ds-flash','messages':[{'role':'user','content':'hi'}]});assert r.status==200
 d=await r.json();assert d['choices'][0]['message']['content']=='fixture answer';assert d['usage']['total_tokens']==5;assert d['bob']['conversation_id']=='fixture-conversation'

@pytest.mark.asyncio
async def test_team_handoff_receipt_and_owner(client,monkeypatch):
 calls=[]
 async def fake_turn(request,seat,text,conversation_id=None,project_id=None):
  calls.append((seat,text))
  answers=['{"tasks":["extract the supplied evidence"]}','worker evidence','critic finding','manager result']
  return {'conversation_id':conversation_id or 'fixture-conversation','text':answers[len(calls)-1],'usage':{},'events':[]}
 monkeypatch.setattr(svc,'turn',fake_turn)
 r=await client.post('/agent-services/runs',headers=headers(),json={'team':'balance-budget','task':'analyze SOURCE_MARKER','max_model_calls':4});assert r.status==202
 d=await r.json();rid=d['id'];await svc.tasks(client.app)[rid]
 r=await client.get('/agent-services/runs/'+rid,headers=headers());d=await r.json();assert d['status']=='completed';assert d['independently_verified'] is False
 assert [s for s,_ in calls]==['bob-gpt-sol-61','bob-ds-flash','bob-claude-opus-55','bob-gpt-sol-61'];assert all('SOURCE_MARKER' in t for _,t in calls)
 assert len(d['receipts'])==4;assert all(x['conversation_id']=='fixture-conversation' for x in d['receipts'])
 r=await client.get('/agent-services/runs/'+rid,headers=headers(sub='other'));assert r.status==404
 assert list(svc.store(client.app).glob('*.json'))

@pytest.mark.asyncio
async def test_restart_marks_interrupted_not_completed(tmp_path):
 root=tmp_path/'runs';root.mkdir();p=root/'fixture.json';p.write_text(json.dumps({'id':'fixture','status':'running','owner':'x'}))
 app=web.Application();svc.setup(app,root=tmp_path)
 assert json.loads(p.read_text())['status']=='interrupted'

@pytest.mark.asyncio
async def test_failed_turn_is_not_successful_completion(client,monkeypatch):
 async def fail(*args,**kwargs): raise svc.ServiceError('provider_timeout',502)
 monkeypatch.setattr(svc,'turn',fail)
 r=await client.post('/v1/chat/completions',headers=headers(),json={'model':'bob-ds-flash','messages':[{'role':'user','content':'hi'}]});assert r.status==502
 assert (await r.json())['error']['code']=='provider_timeout'

@pytest.mark.asyncio
async def test_cancel_active_run_preserves_terminal_receipt(client,monkeypatch):
 entered=asyncio.Event()
 async def slow(*args,**kwargs): entered.set();await asyncio.sleep(60)
 monkeypatch.setattr(svc,'turn',slow)
 r=await client.post('/agent-services/runs',headers=headers(),json={'team':'balance-budget','task':'fixture'});rid=(await r.json())['id'];await entered.wait()
 r=await client.post('/agent-services/runs/'+rid+'/cancel',headers=headers());assert r.status==200;assert (await r.json())['status']=='cancelled'
 assert (await (await client.get('/agent-services/runs/'+rid,headers=headers())).json())['status']=='cancelled'

@pytest.mark.asyncio
async def test_provider_sse_has_finish_and_done(client,monkeypatch):
 async def answer(*args,**kwargs):return {'conversation_id':'fixture','text':'fixture answer','usage':{},'events':[]}
 monkeypatch.setattr(svc,'turn',answer)
 r=await client.post('/v1/chat/completions',headers=headers(),json={'model':'bob-ds-flash','stream':True,'messages':[{'role':'user','content':'hi'}]});assert r.status==200
 text=await r.text();assert text.endswith('data: [DONE]\n\n');assert '"finish_reason": "stop"' in text;assert r.headers['X-Bob-Conversation-ID']=='fixture'

@pytest.mark.asyncio
async def test_token_scope_change_cannot_read_run(client,monkeypatch):
 async def fail(*args,**kwargs):raise svc.ServiceError('fixture_error')
 monkeypatch.setattr(svc,'turn',fail)
 r=await client.post('/agent-services/runs',headers=headers(),json={'team':'balance-budget','task':'fixture'});rid=(await r.json())['id'];await svc.tasks(client.app)[rid]
 changed={'Authorization':'Bearer '+create_agent_token('admin',faces=['researcher'],scope={'may_touch':['different-scope']})}
 assert (await client.get('/agent-services/runs/'+rid,headers=changed)).status==404

@pytest.mark.asyncio
async def test_offline_local_worker_rejected_before_manager_call(client,monkeypatch):
 from unittest.mock import AsyncMock
 call=AsyncMock(side_effect=AssertionError('must not spend a manager call'))
 monkeypatch.setattr(svc,'turn',call)
 r=await client.post('/agent-services/runs',headers=headers(),json={'team':'balance-budget','worker':'bob-qwen-strata','task':'fixture'})
 assert r.status==503;assert call.await_count==0

@pytest.mark.asyncio
async def test_selected_local_worker_is_receipted(client,monkeypatch):
 from unittest.mock import AsyncMock
 monkeypatch.setattr(svc,'local_status',AsyncMock(return_value={'available':True}))
 calls=[]
 async def fake(request,seat,text,conversation_id=None,project_id=None):
  calls.append(seat)
  return {'conversation_id':'fixture','text':'{"tasks":["one task"]}' if len(calls)==1 else 'fixture result','usage':{},'events':[]}
 monkeypatch.setattr(svc,'turn',fake)
 r=await client.post('/agent-services/runs',headers=headers(),json={'team':'balance-budget','worker':'bob-qwen-strata','task':'fixture'});rid=(await r.json())['id'];await svc.tasks(client.app)[rid]
 assert calls==['bob-gpt-sol-61','bob-qwen-strata','bob-claude-opus-55','bob-gpt-sol-61']
 row=await (await client.get('/agent-services/runs/'+rid,headers=headers())).json();assert row['status']=='completed';assert row['receipts'][1]['model_pin']=='qwen3.8-flash-next-iq3_xxs'




@pytest.mark.asyncio
async def test_lapsed_seat_is_disabled_and_catalog_says_so(client):
 r=await client.post('/v1/chat/completions',headers=headers(),json={'model':'bob-kimi-k3','messages':[{'role':'user','content':'hi'}]})
 assert r.status==409;assert (await r.json())['error']['code']=='model_face_unavailable'
 d=await (await client.get('/agent-services/catalog',headers=headers())).json()
 seats={m['id']:m for m in d['model_faces']}
 assert seats['bob-kimi-k3']['enabled'] is False and seats['bob-kimi-k3']['disabled_reason']
 # every runnable team must reference only enabled seats; a non-runnable team must say why
 for team in d['teams']:
  if not team.get('runnable'):
   assert team.get('reason'),team['id'];continue
  for role in ('manager','worker','critic','senior'):
   if team.get(role): assert seats[team[role]]['enabled'] is True,(team['id'],role,team[role])


@pytest.mark.asyncio
async def test_team_with_lapsed_seat_fails_before_any_model_call(client,monkeypatch):
 from unittest.mock import AsyncMock
 call=AsyncMock(side_effect=AssertionError('must not spend a manager call'))
 monkeypatch.setattr(svc,'turn',call)
 monkeypatch.setitem(svc.TEAMS,'balance-budget',dict(svc.TEAMS['balance-budget'],critic='bob-kimi-k3'))
 r=await client.post('/agent-services/runs',headers=headers(),json={'team':'balance-budget','task':'fixture'})
 assert r.status==409
 body=await r.json();assert body['error']['code']=='team_seat_unavailable'
 assert body['error']['detail']['seats']==['bob-kimi-k3']
 assert call.await_count==0
