"""BU12: scripted provider failures, no live models or credentials."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from core.backends.openai_codex import parse_sse, provider_failure, OpenAICodexClient
from core.backends.openai_codex_auth import CodexProviderError
from core.backends.provider_failure import CODE_CATEGORIES, SENTENCES, from_payload, seconds
from core.execution import provider_retry
from core.execution.call_guard import required, charged_call, consume_permit, consume_transport
from core.execution.models import ExecutionError
from test_openai_codex_auth import auth_lane, NOW
from test_openai_codex_transport import pinned, sse, text_item, call_item
from test_workflow_controller import harness, drive, ScriptedModel, mock_redis
from core.execution.workflow_context import current


def stream(*events):
    return httpx.Response(200, text=''.join('data: '+json.dumps(e)+'\n\n' for e in events))


@pytest.mark.parametrize('kind', ['error','response.failed','response.incomplete','response.cancelled'])
@pytest.mark.parametrize('code,expected', list(CODE_CATEGORIES.items()) + [('new_unknown_code','model_stopped')])
async def test_all_terminal_events_keep_codes_and_never_log_content(kind,code,expected,caplog):
    error = {'code':code, 'type':'provider_error_type', 'message':'PRIVATE REQUEST RESPONSE SECRET',
             'retry_after':3, 'headers':{'Authorization':'PRIVATE BEARER'}}
    event = {'type':kind, 'error':error} if kind=='error' else {'type':kind,'response':{'error':error}}
    with pytest.raises(CodexProviderError) as caught:
        await parse_sse(stream({'type':'response.output_text.delta','delta':'PRIVATE PARTIAL'},event),{})
    exc = caught.value
    assert exc.code == expected and exc.failure.event_type == kind
    assert exc.failure.provider_code == code and exc.failure.provider_type == 'provider_error_type'
    assert exc.retry_after == 3
    assert code in caplog.text and kind in caplog.text
    assert 'PRIVATE' not in caplog.text + str(exc) + repr(exc)


@pytest.mark.parametrize('reason,expected', [('max_output_tokens','model_output_limit'),
    ('content_filter','model_content_rules'),('unrecognized_reason','model_stopped')])
async def test_incomplete_reason(reason,expected):
    with pytest.raises(CodexProviderError) as caught:
        await parse_sse(stream({'type':'response.incomplete', 'response':{
            'incomplete_details':{'reason':reason}}}),{})
    assert caught.value.code == expected
    assert caught.value.failure.incomplete_reason == reason
    assert not caught.value.failure.retryable


async def test_top_level_error_code_and_type_and_reset():
    with pytest.raises(CodexProviderError) as caught:
        await parse_sse(stream({'type':'error','code':'usage_limit_reached',
            'resets_at':1900000000,'message':'PRIVATE'}),{})
    assert caught.value.failure.provider_type is None
    assert caught.value.failure.sentence == "The plan's usage limit has been reached; it resets at 2030-03-17 17:46:40 UTC."
    assert from_payload({'error':{'type':'usage_limit_reached'}}).code == 'model_usage_limit'


@pytest.mark.parametrize('body,event,expected', [
    ('data: {broken\n\n','stream.malformed','model_stopped'),
    ('data: []\n\n','stream.malformed','model_stopped'),
    ('data: {"type":"response.output_text.delta","delta":"PRIVATE"}\n\n',
     'stream.ended_without_completion','model_provider_error'),
])
async def test_malformed_and_truncated_streams(body,event,expected,caplog):
    with pytest.raises(CodexProviderError) as caught:
        await parse_sse(httpx.Response(200,text=body),{})
    assert caught.value.code == expected and caught.value.failure.event_type == event
    assert 'PRIVATE' not in caplog.text


@pytest.mark.parametrize('code,expected', [(None,'model_rate_limited'),
    ('usage_limit_reached','model_usage_limit'),('context_length_exceeded','model_context_limit'),
    ('not_a_known_code','model_stopped')])
async def test_http_429_preserves_delay_and_reason(auth_lane,code,expected,caplog):
    lane,_ = auth_lane
    def endpoint(request):
        return httpx.Response(429,headers={'Retry-After':'7','Authorization':'PRIVATE'},
            json={'error':{'code':code,'message':'PRIVATE BODY'}})
    client=OpenAICodexClient(lane=pinned(lane),clock=lambda:NOW,transport=httpx.MockTransport(endpoint))
    with pytest.raises(CodexProviderError) as caught:
        await client.complete([])
    assert caught.value.code == expected and caught.value.retry_after == 7
    assert caught.value.status == 429 and 'PRIVATE' not in caplog.text + str(caught.value)


def test_delay_formats_and_unknown_code_fails_closed():
    f=from_payload({'error':{'code':'rate_limit_exceeded','message':'Please try again in 1250 ms.',
        'headers':{'Retry-After':'2'},'retry_after_ms':3000}})
    assert f.retry_after == 3
    assert seconds('Wed, 18 Nov 2026 12:00:05 GMT',now=1795003200) == 5
    assert seconds('nan') is None and seconds('garbage') is None
    assert from_payload({'error':{'code':'unknown'}},status=500).code == 'model_stopped'
    assert from_payload({'error':{'code':'rate_limit_exceeded','message':'SECRET try again in 2 seconds'}}).retry_after == 2


@pytest.mark.parametrize('code', SENTENCES)
async def test_retry_decisions_and_attempt_bound(monkeypatch,code):
    failure=from_payload({'error':{'code':next((k for k,v in CODE_CATEGORIES.items() if v==code),'unknown')}})
    if code=='model_output_limit': failure=from_payload({'incomplete_details':{'reason':'max_output_tokens'}})
    if code=='model_auth_rejected': failure=from_payload(status=401)
    exc=CodexProviderError(failure.sentence,code=failure.code,failure=failure)
    call=AsyncMock(side_effect=exc); wait=AsyncMock()
    monkeypatch.setattr(provider_retry,'sleep',wait)
    with pytest.raises(CodexProviderError): await provider_retry.model_call(call)
    assert call.await_count == (3 if failure.retryable else 1)
    assert [c.args[0] for c in wait.await_args_list] == ([1,2] if failure.retryable else [])


@pytest.mark.parametrize('delay,remaining,allowed,expected',[(12,20,True,[12]),
    (30,40,True,[30]),(31,60,True,[]),(4,4,True,[]),(1,20,False,[])])
async def test_backoff_honors_delay_cap_time_and_call_budget(monkeypatch,delay,remaining,allowed,expected):
    error=provider_failure({'error':{'code':'rate_limit_exceeded','retry_after':delay}})
    call=AsyncMock(side_effect=[error,'ok']); wait=AsyncMock(); events=[]
    monkeypatch.setattr(provider_retry,'sleep',wait)
    if expected:
        assert await provider_retry.model_call(call,writer=events.append,
            remaining=AsyncMock(return_value=remaining),can_retry=lambda:allowed) == 'ok'
        assert events[0]['wait_seconds'] == delay and 'waiting on the model' in events[0]['args']
    else:
        with pytest.raises(CodexProviderError):
            await provider_retry.model_call(call,remaining=AsyncMock(return_value=remaining),can_retry=lambda:allowed)
    assert [c.args[0] for c in wait.await_args_list] == expected


@pytest.fixture
def chat_loop(monkeypatch):
    from core.nodes import execute as ex
    tools=[]; events=[]
    registry=SimpleNamespace(get_allowed_tools=lambda face:['get_server_time'],list_skills=lambda face:[],
        get_face=lambda face:SimpleNamespace(tool_loop_max_rounds=10))
    monkeypatch.setattr('core.faces.registry.get_default_registry',lambda:registry)
    monkeypatch.setattr('core.faces.prompt.interactive_messages',lambda messages,*args:messages)
    monkeypatch.setattr('core.context.enforce_backend_limits',lambda messages,backend:messages)
    monkeypatch.setattr(ex,'check_tool_access',lambda *args:True)
    monkeypatch.setattr(ex,'require_backend',lambda *args:None)
    monkeypatch.setattr(ex,'get_all_tools',AsyncMock(side_effect=lambda permitted:tools))
    monkeypatch.setattr(provider_retry,'sleep',AsyncMock())
    monkeypatch.setattr(ex,'_append_agent_turn_event',AsyncMock())
    monkeypatch.setattr(ex,'_get_stream_writer',lambda:events.append)
    return SimpleNamespace(ex=ex,tools=tools,events=events,registry=registry)


async def test_completed_work_survives_exhausted_model_retries(chat_loop,monkeypatch):
    h=chat_loop; calls=[]
    @tool
    async def get_server_time() -> str:
        """Read the clock."""
        calls.append('ran'); return 'PRIVATE TOOL RESULT'
    h.tools.append(get_server_time)
    model=SimpleNamespace(bind_tools=lambda tools:model,ainvoke=AsyncMock(side_effect=[
        AIMessage(content='I checked the clock.',tool_calls=[{'name':'get_server_time','args':{},'id':'clock'}]),
        *[provider_failure({'error':{'code':'server_error'}}) for _ in range(3)]]))
    monkeypatch.setattr(h.ex,'_build_tool_model',lambda *args,**kwargs:model)
    monkeypatch.setattr(h.ex,'_is_tool_enabled',lambda *args:True)
    result=await h.ex.execute_node({'face_id':'assistant','backend':'deepseek_v4_flash','messages':[]})
    text=result['messages'][-1]['content']
    assert text.startswith('I checked the clock.') and 'Completed tool steps: get server time (1).' in text
    assert SENTENCES['model_provider_error'] in text and 'continue' in text
    assert result['stopped_reason']=='model_provider_error' and result['error'] is None
    assert calls==['ran'] and model.ainvoke.await_count==4
    assert 'PRIVATE' not in text
    assert any(e.get('tool')=='get_server_time' and e.get('phase')=='result' for e in h.events)
    assert h.ex._append_agent_turn_event.await_args.kwargs['assistant_response']==text


async def test_chat_retries_count_against_step_budget(chat_loop,monkeypatch):
    h=chat_loop
    h.registry.get_face=lambda face:SimpleNamespace(tool_loop_max_rounds=2)
    model=SimpleNamespace(bind_tools=lambda tools:model,
        ainvoke=AsyncMock(side_effect=provider_failure({'error':{'code':'rate_limit_exceeded'}})))
    monkeypatch.setattr(h.ex,'_build_tool_model',lambda *args,**kwargs:model)
    # A tool must be bound even though this model never returns a completed round.
    h.tools.append(SimpleNamespace(name='get_server_time'))
    result=await h.ex._run_tool_loop([],'deepseek_v4_flash','assistant',h.events.append)
    assert result.stopped_reason=='model_rate_limited'
    assert model.ainvoke.await_count==2 and provider_retry.sleep.await_count==1


async def test_tool_failure_cannot_retry_the_round(chat_loop,monkeypatch):
    h=chat_loop; ran=[]
    @tool
    async def get_server_time() -> str:
        """Read the clock."""
        ran.append(1); raise provider_failure({'error':{'code':'server_error'}})
    h.tools.append(get_server_time)
    model=SimpleNamespace(bind_tools=lambda tools:model,ainvoke=AsyncMock(side_effect=[
        AIMessage(content='',tool_calls=[{'name':'get_server_time','args':{},'id':'clock'}]),AIMessage(content='Tool failed.')]))
    monkeypatch.setattr(h.ex,'_build_tool_model',lambda *args,**kwargs:model)
    assert await h.ex._run_tool_loop([],'deepseek_v4_flash','assistant',None)=='Tool failed.'
    assert ran==[1] and model.ainvoke.await_count==2 and provider_retry.sleep.await_count==0


async def test_failed_stream_tool_item_is_not_executed_on_retry(auth_lane,chat_loop,monkeypatch):
    from core.backends.openai_codex import ChatOpenAICodex
    lane,_=auth_lane; h=chat_loop; ran=[]; requests=[]
    @tool
    async def get_server_time() -> str:
        """Read the clock."""
        ran.append(1);return 'clock read'
    h.tools.append(get_server_time)
    def endpoint(request):
        requests.append(request)
        if len(requests)==1:
            return stream({'type':'response.output_item.done','output_index':0,'item':call_item('get_server_time','{}')},
                {'type':'response.failed','response':{'error':{'code':'server_error'}}})
        if len(requests)==2: return sse([call_item('get_server_time','{}')])
        return sse([text_item('Done.')])
    client=OpenAICodexClient(lane=pinned(lane),clock=lambda:NOW,transport=httpx.MockTransport(endpoint))
    monkeypatch.setattr(h.ex,'_build_tool_model',lambda *args,**kwargs:ChatOpenAICodex(client=client))
    assert await h.ex._run_tool_loop([],'openai_codex','assistant',None)=='Done.'
    assert ran==[1] and len(requests)==3


async def test_workflow_retries_have_separate_permits_and_durable_charges(harness,monkeypatch):
    original=ScriptedModel.ainvoke; seen=[]
    monkeypatch.setattr(provider_retry,'sleep',AsyncMock())
    async def invoke(model,messages):
        ctx=current.get()
        if ctx.stage['key']=='plan':
            consume_transport(); seen.append(ctx.model_calls)
            if len(seen)==1: raise provider_failure({'error':{'code':'rate_limit_exceeded'}})
        return await original(model,messages)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new(); result=await drive(harness,row)
    assert result['status']=='completed',result['reason']
    assert seen==[1,2]
    plan=next(s for s in result['stages'] if s['key']=='plan')
    assert plan['calls']==2 and result['budget']['unknown_calls']==0
    failed=[a for a in harness.store.pool.attempts.values() if a['state']=='failed']
    assert len(failed)==1 and failed[0]['failure_detail']['code']=='model_rate_limited'
    events=harness.store.events['lane','owner',row['id']]
    assert any(e['kind']=='tool_activity' and e['payload'].get('tool')=='model_wait' for e in events)


@pytest.mark.parametrize('code', ['usage_limit_reached','context_length_exceeded','cyber_policy','unknown'])
async def test_workflow_terminal_reason_is_plain_and_fixed(harness,monkeypatch,code):
    original=ScriptedModel.ainvoke; calls=[]
    async def invoke(model,messages):
        if current.get().stage['key']=='plan':
            calls.append(1);raise provider_failure({'error':{'code':code}})
        return await original(model,messages)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new(); result=await drive(harness,row)
    failure=from_payload({'error':{'code':code}})
    assert result['status']=='failed' and result['reason_code']==failure.code
    assert result['reason']==result['report']['reason']==failure.sentence
    plan=next(s for s in result['stages'] if s['key']=='plan')
    assert plan['reason']==failure.sentence and plan['reason_code']==failure.code and calls==[1]
    report_plan=next(s for s in result['report']['stages'] if s['key']=='plan')
    assert report_plan['reason_code']==failure.code


async def test_workflow_call_budget_is_not_bypassed(harness,monkeypatch):
    original=ScriptedModel.ainvoke; charge=harness.controller.charge; seen=[]
    monkeypatch.setattr(provider_retry,'sleep',AsyncMock())
    async def limited(ctx,messages):
        if seen: raise ExecutionError('call_budget_exhausted',409)
        return await charge(ctx,messages)
    async def invoke(model,messages):
        if current.get().stage['key']=='plan':
            seen.append(1); raise provider_failure({'error':{'code':'server_error'}})
        return await original(model,messages)
    monkeypatch.setattr(harness.controller,'charge',limited)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new(); result=await drive(harness,row)
    assert result['reason_code']=='call_budget_exhausted' and seen==[1]


async def test_a_single_permit_still_rejects_hidden_second_transport():
    guard=required.set(True)
    try:
        with charged_call('one'):
            consume_permit();consume_transport()
            with pytest.raises(ExecutionError,match='unreserved_model_call'):consume_transport()
    finally: required.reset(guard)


@pytest.mark.parametrize('status,expected',[(429,'model_rate_limited'),(500,'model_provider_error'),(401,'model_auth_rejected')])
async def test_api_key_http_failures_use_same_user_sentences(monkeypatch,status,expected):
    import aiohttp
    from core.nodes import execute as ex
    monkeypatch.setattr(ex,'_append_agent_turn_event',AsyncMock())
    result=await ex._execution_failure({'backend':'claude_api'},
        aiohttp.ClientResponseError(SimpleNamespace(real_url='PRIVATE URL'),(),status=status,message='PRIVATE BODY'))
    assert result['messages'][0]['content']==SENTENCES[expected]
    assert result['error']==expected and 'internal error' not in result['messages'][0]['content']


@pytest.mark.parametrize('code,expected', [('usage_limit_reached','forbidden'),
    ('context_length_exceeded','forbidden'),('cyber_policy','forbidden'),
    ('server_error','transient'),('rate_limit_exceeded','transient'),('unknown','forbidden')])
def test_legacy_recovery_honors_fixed_category_before_http_status(code,expected):
    from core.execution.failure import classify
    error=provider_failure({'error':{'code':code}},status=429)
    assert classify(error)==(expected,error.code)


@pytest.mark.parametrize('event', [
    {'type':'response.output_text.delta','delta':42},
    {'type':'response.output_item.done','output_index':'private','item':{}},
    {'type':'response.completed','response':{'output':[{'type':'message','content':[{'type':'output_text','text':42}]}]}},
    {'type':'response.completed','response':None},
])
async def test_malformed_shapes_always_keep_fixed_error(event):
    with pytest.raises(CodexProviderError) as caught:
        await parse_sse(stream(event),{})
    assert caught.value.code=='model_stopped'


async def test_stage_wait_is_flushed_before_sleep(harness,monkeypatch):
    original=ScriptedModel.ainvoke; seen=[]
    async def invoke(model,messages):
        if current.get().stage['key']=='plan' and not seen:
            seen.append(1);raise provider_failure({'error':{'code':'server_error'}})
        return await original(model,messages)
    async def wait(delay):
        ctx=current.get()
        assert not ctx.waiting  # Model delay consumes active time; it is not owner wait.
        events=harness.store.events['lane','owner',ctx.row['id']]
        assert any(e['kind']=='tool_activity' and e['payload'].get('wait_seconds')==delay for e in events)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    monkeypatch.setattr(provider_retry,'sleep',wait)
    row=await harness.new();result=await drive(harness,row)
    assert result['status']=='completed'


async def test_stage_retry_stops_when_remaining_time_cannot_cover_delay(harness,monkeypatch):
    original=ScriptedModel.ainvoke;remaining=harness.controller.remaining;seen=[]
    monkeypatch.setattr(provider_retry,'sleep',AsyncMock())
    async def budget(ctx):
        if seen and ctx.stage['key']=='plan': return 0.5
        return await remaining(ctx)
    async def invoke(model,messages):
        if current.get().stage['key']=='plan':
            seen.append(1);raise provider_failure({'error':{'code':'server_error'}})
        return await original(model,messages)
    monkeypatch.setattr(harness.controller,'remaining',budget)
    monkeypatch.setattr(ScriptedModel,'ainvoke',invoke)
    row=await harness.new();result=await drive(harness,row)
    assert result['status']=='failed' and result['reason_code']=='model_provider_error'
    assert seen==[1] and provider_retry.sleep.await_count==0


def test_unknown_code_is_never_overridden_by_status_or_incomplete_reason():
    assert from_payload({'error':{'code':'future_code'},
        'incomplete_details':{'reason':'max_output_tokens'}},status=500).code=='model_stopped'


async def test_identifier_fields_cannot_inject_content_into_logs(caplog):
    error=provider_failure({'error':{'code':'PRIVATE prose\nforged log','type':'PRIVATE prose',
        'retry_after':'PRIVATE prose','message':'PRIVATE prose'},'incomplete_details':{'reason':'PRIVATE prose'}},status=429)
    assert error.code=='model_stopped'
    assert 'PRIVATE' not in caplog.text + str(error)
