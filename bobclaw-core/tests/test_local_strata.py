import json
from unittest.mock import AsyncMock
import pytest
from core.backends.local_strata import LocalStrataClient,LocalStrataOffline,LocalStrataError,MODEL
from core.nodes import execute,route
from core.graph import _route_after_recall

@pytest.mark.asyncio
async def test_local_strict_route_and_rejection_are_fail_closed():
 state={'agent_service_single_turn':True,'backend_override':'qwen_research','model_override':MODEL,'face_id':'researcher','task':'Reply hi'}
 result=await route.route_node(state);assert result.get('error') is None;assert result['backend']=='qwen_research'
 bad=await route.route_node({**state,'model_override':'wrong-model'})
 assert bad['error'];assert _route_after_recall({**state,**bad})=='__end__'


@pytest.mark.asyncio
async def test_strata_face_does_not_hit_other_local_backend(monkeypatch):
 chat=AsyncMock(return_value='local answer');monkeypatch.setattr(LocalStrataClient,'chat',chat)
 monkeypatch.setattr(execute,'_stream_and_collect',AsyncMock(side_effect=AssertionError('wrong local/cloud path')))
 monkeypatch.setattr(execute,'_append_agent_turn_event',AsyncMock())
 row=await execute.execute_node({'agent_service_single_turn':True,'backend':'qwen_research','model_override':MODEL,'task':'Reply hi','messages':[],'face_id':'researcher'})
 assert row['error'] is None;assert chat.await_count==1

@pytest.mark.asyncio
async def test_offline_is_explicit_no_fallback(monkeypatch):
 monkeypatch.setattr(LocalStrataClient,'chat',AsyncMock(side_effect=LocalStrataOffline()))
 fallback=AsyncMock(side_effect=AssertionError('cloud spill'));monkeypatch.setattr(execute,'_stream_and_collect',fallback)
 monkeypatch.setattr(execute,'_append_agent_turn_event',AsyncMock())
 row=await execute.execute_node({'agent_service_single_turn':True,'backend':'qwen_research','model_override':MODEL,'task':'Reply hi','messages':[],'face_id':'researcher'})
 assert row['error']=='LocalStrataOffline';assert fallback.await_count==0

def test_model_identity_and_empty_answers_fail_closed():
 d={'model':MODEL,'choices':[{'message':{'content':'answer'}}]}
 assert LocalStrataClient.parse(json.dumps(d),0)=='answer'
 d['model']='wrong'
 with pytest.raises(LocalStrataError):LocalStrataClient.parse(json.dumps(d),0)
 with pytest.raises(LocalStrataOffline):LocalStrataClient.parse('{"error":"offline"}',0)
 with pytest.raises(LocalStrataOffline):LocalStrataClient.parse('{}',1)


@pytest.mark.asyncio
@pytest.mark.parametrize("windows", [False, True])
async def test_missing_bridge_command_refuses_before_ssh(monkeypatch, windows):
 from core.backends import claude_pc as pc
 host="example-windows" if windows else "example-linux"
 monkeypatch.setattr(pc,"WINDOWS_HOST","example-windows")
 monkeypatch.setattr(pc,"choose_host",AsyncMock(return_value=host))
 monkeypatch.setattr("core.backends.local_strata.execution_mode",lambda:"legacy")
 setting="BOB_STRATA_WINDOWS_COMMAND" if windows else "BOB_STRATA_LINUX_COMMAND"
 monkeypatch.delenv(setting,raising=False)
 with pytest.raises(LocalStrataOffline,match="set "+setting):
  await LocalStrataClient().status()
