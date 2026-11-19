import pytest
from unittest.mock import AsyncMock
from core.graph import _route_after_recall
from core.nodes import decompose,route,execute

@pytest.mark.asyncio
async def test_single_turn_skips_decomposition_and_profiles(monkeypatch):
 llm=AsyncMock(side_effect=AssertionError('hidden decomposition call'));monkeypatch.setattr(decompose,'_call_llm',llm)
 state={'agent_service_single_turn':True,'task':'Plan and build and test ten parallel tasks','backend_override':'kimi_code','model_override':'k3','profile_name':'council-spec-round3','face_id':'researcher'}
 result=await decompose.decompose_node(state);assert result.get('subtasks')==[];assert llm.await_count==0
 resolved=await route.route_node(state);assert resolved['backend']=='kimi_code';assert not resolved.get('council_spec');assert resolved['escalation_backend'] is None
 assert _route_after_recall({**state,'council_spec':{},'research_request':True})=='execute'

@pytest.mark.asyncio
async def test_single_turn_never_uses_escalation_pin(monkeypatch):
 pin=AsyncMock(side_effect=AssertionError('unexpected escalation'));monkeypatch.setattr(execute,'_check_escalation_pin',pin)
 stream=AsyncMock(return_value='strict answer');monkeypatch.setattr(execute,'_stream_and_collect',stream)
 monkeypatch.setattr(execute,'_append_agent_turn_event',AsyncMock())
 row=await execute.execute_node({'agent_service_single_turn':True,'backend':'kimi_code','model_override':'k3','task':'Reply hi','messages':[],'face_id':'researcher'})
 assert row['error'] is None;assert stream.await_args.args[1:3]==('kimi_code','k3');assert pin.await_count==0

@pytest.mark.asyncio
async def test_single_turn_429_is_not_paid_fallback(monkeypatch):
 import aiohttp
 stream=AsyncMock(side_effect=aiohttp.ClientResponseError(None,(),status=429,message='quota'));monkeypatch.setattr(execute,'_stream_and_collect',stream)
 monkeypatch.setattr(execute,'_append_agent_turn_event',AsyncMock())
 row=await execute.execute_node({'agent_service_single_turn':True,'backend':'kimi_code','model_override':'k3','task':'Reply hi','messages':[],'face_id':'researcher'})
 assert row['error'];assert stream.await_count==1

@pytest.mark.asyncio
async def test_opus_face_uses_pc_not_local_claude(monkeypatch):
 from core.backends.claude_pc import ClaudePCClient
 pc=AsyncMock(return_value='pc answer');monkeypatch.setattr(ClaudePCClient,'chat',pc)
 monkeypatch.setattr(execute,'_stream_and_collect',AsyncMock(side_effect=AssertionError('local Claude forbidden')))
 monkeypatch.setattr(execute,'_append_agent_turn_event',AsyncMock())
 row=await execute.execute_node({'agent_service_single_turn':True,'backend':'claude_code','model_override':'claude-opus-5-5','task':'Reply hi','messages':[],'face_id':'researcher'})
 assert row['error'] is None;assert pc.await_count==1

def test_pc_response_model_mismatch_fails_closed():
 import json
 from core.backends.claude_pc import ClaudePCClient,ClaudePCError
 good={'subtype':'success','is_error':False,'result':'answer','modelUsage':{'claude-opus-5-5':{}}}
 assert ClaudePCClient.parse(json.dumps(good),0)=='answer'
 good['modelUsage']={'claude-sonnet-other':{}}
 with pytest.raises(ClaudePCError):ClaudePCClient.parse(json.dumps(good),0)
 with pytest.raises(ClaudePCError):ClaudePCClient.parse('{}',1)

@pytest.mark.asyncio
async def test_sonnet_face_uses_pc_sonnet_client(monkeypatch):
 from core.backends.claude_pc import ClaudePCClient,ClaudePCSonnetClient
 sonnet=AsyncMock(return_value='sonnet answer');monkeypatch.setattr(ClaudePCSonnetClient,'chat',sonnet)
 opus=AsyncMock(side_effect=AssertionError('opus client must not serve a sonnet pin'));monkeypatch.setattr(ClaudePCClient,'chat',opus)
 monkeypatch.setattr(execute,'_stream_and_collect',AsyncMock(side_effect=AssertionError('local Claude forbidden')))
 monkeypatch.setattr(execute,'_append_agent_turn_event',AsyncMock())
 row=await execute.execute_node({'agent_service_single_turn':True,'backend':'claude_code','model_override':'claude-sonnet-5-5','task':'Reply hi','messages':[],'face_id':'researcher'})
 assert row['error'] is None;assert sonnet.await_count==1;assert opus.await_count==0

def test_pc_sonnet_response_model_contract_fails_closed():
 import json
 from core.backends.claude_pc import ClaudePCClient,ClaudePCSonnetClient,ClaudePCError
 good={'subtype':'success','is_error':False,'result':'answer','modelUsage':{'claude-sonnet-5-5':{}}}
 assert ClaudePCSonnetClient.parse(json.dumps(good),0)=='answer'
 # sonnet payload must NOT satisfy the opus client, and vice versa
 with pytest.raises(ClaudePCError):ClaudePCClient.parse(json.dumps(good),0)
 good['modelUsage']={'claude-opus-5-5':{}}
 with pytest.raises(ClaudePCError):ClaudePCSonnetClient.parse(json.dumps(good),0)

