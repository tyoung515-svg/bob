import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from core.mcp.server import build_server,MCPConfig

@pytest.mark.asyncio
async def test_team_tools_registration_permissions():
 names={t.name for t in build_server(MCPConfig('fixture','http://unused',['researcher']))._tool_manager.list_tools()}
 assert {'list_agent_services','start_team_run','get_team_run','message_team_manager','cancel_team_run'}<=names
 assert 'prepare_execution_grant' in names
 assert not {'issue_execution_grant','confirm_owner','owner_confirm','revoke_execution_grant'} & names
 limited={t.name for t in build_server(MCPConfig('fixture','http://unused',['assistant']))._tool_manager.list_tools()}
 assert 'start_team_run' not in limited

@pytest.mark.asyncio
async def test_start_team_run_real_http_serialization():
 hits=[]
 async def accept(request):
  hits.append((request.headers.get('Authorization'),await request.json()));return web.json_response({'id':'fixture','status':'accepted'},status=202)
 app=web.Application();app.router.add_post('/agent-services/runs',accept)
 server=TestServer(app);await server.start_server()
 try:
  mcp=build_server(MCPConfig('fixture-token',str(server.make_url('/')).rstrip('/'),['researcher']))
  tools={t.name:t for t in mcp._tool_manager.list_tools()}
  answer=await tools['start_team_run'].run({'team':'balance-budget','task':'source evidence'})
  assert 'accepted' in str(answer);assert hits==[('Bearer fixture-token',{'team':'balance-budget','task':'source evidence','max_model_calls':4,'max_seconds':900,'require_bulk':False})]
 finally:await server.close()


@pytest.mark.asyncio
async def test_v2_tools_real_http_keys_budget_and_server_list():
 hits=[]
 async def receive(request):
  body=await request.json() if request.can_read_body else None
  hits.append((request.method,request.path,dict(request.query),body))
  return web.json_response({'source':'server','run':{'status':'accepted'}})
 app=web.Application();app.router.add_route('*','/{tail:.*}',receive)
 server=TestServer(app);await server.start_server()
 try:
  mcp=build_server(MCPConfig('fixture-token',str(server.make_url('/')).rstrip('/'),['researcher']))
  registered={t.name:t for t in mcp._tool_manager.list_tools()}
  assert {'list_saved_teams','save_team','start_team_run_v2','get_team_run_v2','list_team_runs','cancel_team_run_v2'}<=registered.keys()
  limited={t.name for t in build_server(MCPConfig('fixture','http://unused',['assistant']))._tool_manager.list_tools()}
  assert 'start_team_run_v2' not in limited and 'save_team' not in limited
  await registered['save_team'].run({'definition':{'id':'draft'},'expected_revision':None,'request_key':'save-key'})
  assert hits[-1][3]=={'contract_version':2,'definition':{'id':'draft'},'expected_revision':None,'request_key':'save-key'}
  await registered['start_team_run_v2'].run({'team':'configured','team_revision':7,'task':'SOURCE','request_key':'start-key',
                                            'start_calls':1000,'wall_mode':'none','wall_seconds':None,'evidence_ids':['SOURCE']})
  assert hits[-1][3]['budget']=={'start_calls':1000,'wall_mode':'none','wall_seconds':None}
  assert hits[-1][3]['request_key']=='start-key' and hits[-1][3]['team_revision']==7
  await registered['list_team_runs'].run({})
  assert hits[-1][1]=='/agent-services/runs' and hits[-1][2]['contract_version']=='2'
  rid='00000000-0000-0000-0000-000000000001'
  await registered['get_team_run_v2'].run({'run_id':rid,'after_event_seq':3})
  assert hits[-1][2]=={'contract_version':'2','after_event_seq':'3'}
  await registered['cancel_team_run_v2'].run({'run_id':rid,'request_key':'cancel-key'})
  assert hits[-1][3]=={'contract_version':2,'request_key':'cancel-key'}
  contract={'criteria':[{'id':'answer','output_kind':'answer','checks':{}}]}
  await registered['start_team_run_v2'].run({'team':'loop','team_revision':1,'task':'SOURCE','request_key':'loop-key','acceptance':contract})
  assert hits[-1][3]['acceptance']==contract
  await registered['resume_team_run'].run({'run_id':rid,'expected_revision':42,'request_key':'resume-key','budget':{'start_calls':100}})
  assert hits[-1][1].endswith('/resume') and hits[-1][3]=={'contract_version':2,'expected_revision':42,'request_key':'resume-key','budget':{'start_calls':100}}
  await registered['set_project_call_budget'].run({'project_id':'p','call_limit':500,'expected_revision':None,'request_key':'pool-key'})
  assert hits[-1][1]=='/agent-services/budgets' and hits[-1][3]['expected_revision'] is None
  await registered['get_team_run_v2'].run({'run_id':rid,'receipt_cursor':1200})
  assert hits[-1][2]['receipt_cursor']=='1200'
  await registered['prepare_execution_grant'].run({'request_key':'proposal-key','request':{'task':'S1'}})
  assert hits[-1][1]=='/agent-services/grant-proposals' and hits[-1][3]=={'contract_version':2,'request_key':'proposal-key','request':{'task':'S1'}}
 finally:await server.close()
