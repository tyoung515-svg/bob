"""Real Claude adapter + shared tool loop; deterministic fake stream-json CLI."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from core.backends.claude_turn import ClaudeTurn, ClaudeTurnError, failure
from core.nodes import execute
from cc2_fake_cli import script, MODEL


class FakeLink:
    """In-process wire peer running the exact same script as the fake CLI program."""
    def __init__(self, operations):
        self.operations = operations
        self.reader = asyncio.StreamReader(limit=4*1024*1024+1)
        self.responses = []
        self.pending = {}
        self.task = None
        self.value = None
        self.stopped = False
        self.serial = 0

    async def connect(self):
        return self.reader, self

    def event(self, kind, event):
        self.reader.feed_data((json.dumps({'type': kind, 'event': event})+'\n').encode())

    def write(self, data):
        value = json.loads(data)
        if 'version' in value:
            self.value = value
            self.task = asyncio.create_task(self.run())
        elif value['type'] == 'mcp':
            response = value['response']
            self.responses.append(response)
            pending = self.pending.pop(response['id'], None)
            if pending and not pending.done():
                pending.set_result(response)
        elif value['type'] == 'stop':
            self.stopped = True
            if self.task and not self.task.done():
                self.task.cancel()

    async def run(self):
        async def emit(event):
            if event is None:
                self.reader.feed_data(b'{bad stream\n')
            else:
                self.event('cli', event)
        async def rpc(method, params):
            self.serial += 1
            cid = self.serial
            future = asyncio.get_running_loop().create_future()
            self.pending[cid] = future
            self.event('mcp', {'jsonrpc': '2.0', 'id': cid, 'method': method, 'params': params})
            return await future
        try:
            await script(self.operations, emit, rpc)
        except asyncio.CancelledError:
            pass
        finally:
            self.reader.feed_data((json.dumps({'type': 'terminal', 'returncode': 0,
                'reason': 'cancelled' if self.stopped else None, 'cleanup': 'idle_proved'})+'\n').encode())

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        if self.task and not self.task.done():
            self.task.cancel()

    async def wait_closed(self):
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'worker')
    monkeypatch.setattr(execute, 'require_backend', lambda *a: None)
    monkeypatch.setattr('core.lane_config.require_backend', lambda *a: None)
    records, frames, links, turns, effects = [], [], [], [], []
    @tool
    async def get_server_time() -> str:
        """Return the fixed test time through the real tool loop."""
        effects.append('time')
        return 'fixed time'
    monkeypatch.setattr(execute, 'get_all_tools', AsyncMock(return_value=[get_server_time]))
    def install(operations):
        def factory(**kwargs):
            link = FakeLink(operations)
            turn = ClaudeTurn(**kwargs, connector=link.connect,
                recorder=lambda tid, kind, payload: records.append((tid, kind, payload)))
            links.append(link)
            turns.append(turn)
            return turn
        monkeypatch.setattr('core.backends.claude_turn.ClaudeTurn', factory)
    async def run(operations, **kwargs):
        install(operations)
        return await execute._run_tool_loop([{'role': 'user', 'content': 'Read the time'}],
            'claude_code', 'assistant-tools', frames.append, model_override='sonnet', **kwargs)
    return SimpleNamespace(run=run, install=install, records=records, frames=frames,
        links=links, turns=turns, effects=effects, tool=get_server_time)


@pytest.mark.asyncio
async def test_plain_partial_thinking_usage_identity_and_filtered_list(rig):
    answer = await rig.run([{'text': 'Hello'}])
    assert answer == 'Hello'
    assert ''.join(e['content'] for e in rig.frames if e['type'] == 'token') == answer
    assert 'PRIVATE' not in str(rig.frames)
    assert len([e for e in rig.frames if e['type'] == 'token']) == 5
    listed = next(r['result']['tools'] for r in rig.links[0].responses if 'tools' in r.get('result', {}))
    assert listed == rig.links[0].value['tools']
    assert [t['name'] for t in listed] == ['get_server_time']
    assert any(kind == 'model' and p['reported'] == MODEL for _, kind, p in rig.records)
    usage = next(p for _, kind, p in rig.records if kind == 'usage')
    assert usage['input_tokens'] == 14 and usage['output_tokens'] == 7
    assert usage['billing'] == 'subscription' and 'cost' not in str(usage)


@pytest.mark.asyncio
async def test_tools_and_several_rounds_use_same_invocation(rig):
    ops = [{'text': 'Checking'}, {'tools': [{'name': 'get_server_time'}, {'name': 'get_server_time'}]},
           {'tools': [{'name': 'get_server_time'}]}, {'text': 'Done'}]
    answer = await rig.run(ops)
    assert answer == 'Checking\n\nDone'
    assert ''.join(e['content'] for e in rig.frames if e['type'] == 'token') == answer
    assert rig.effects == ['time'] * 3 and len(rig.links) == 1
    assert len([p for _, kind, p in rig.records if kind == 'tool_result']) == 3
    assert rig.turns[0].closed


@pytest.mark.asyncio
async def test_unknown_tool_is_recorded_refused_and_can_recover(rig):
    answer = await rig.run([{'tools': [{'name': 'Bash', 'args': {'command': 'denied'}}]}, {'text': 'Recovered'}])
    assert answer == 'Recovered' and not rig.effects
    results = [p for _, kind, p in rig.records if kind == 'tool_result']
    assert 'not available' in results[0]['content']


@pytest.mark.asyncio
async def test_held_tool_can_resolve_and_stop_mid_tool(rig, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    @tool
    async def get_server_time() -> str:
        """Hold the existing tool await until its approval resolves."""
        entered.set()
        await release.wait()
        return 'approved'
    monkeypatch.setattr(execute, 'get_all_tools', AsyncMock(return_value=[get_server_time]))
    task = asyncio.create_task(rig.run([{'tools': [{'name': 'get_server_time'}]}, {'text': 'Done'}]))
    await asyncio.wait_for(entered.wait(), 2)
    assert not task.done() and len(rig.links[0].responses) == 2
    release.set()
    assert await task == 'Done'
    entered.clear(); release.clear()
    task = asyncio.create_task(rig.run([{'tools': [{'name': 'get_server_time'}]}, {'text': 'Never'}]))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert rig.links[-1].stopped and rig.turns[-1].closed
    assert not any(e.get('content') == 'Never' for e in rig.frames)


@pytest.mark.asyncio
async def test_stop_mid_answer_ends_cli(rig):
    task = asyncio.create_task(rig.run([{'text': 'Started'}, {'wait': 60}, {'text': 'Never'}]))
    async with asyncio.timeout(2):
        while not any(e.get('content') == 'S' for e in rig.frames): await asyncio.sleep(.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert rig.links[0].stopped and rig.turns[0].closed


@pytest.mark.asyncio
async def test_step_question_extends_same_cli(rig, monkeypatch):
    from core.faces.registry import get_default_registry
    monkeypatch.setattr(get_default_registry().get_face('assistant-tools'), 'tool_loop_max_rounds', 1)
    question = AsyncMock(return_value=True)
    monkeypatch.setattr('core.tools.ask.can_request_step_extension', lambda: True)
    monkeypatch.setattr('core.tools.ask.request_step_extension', question)
    answer = await rig.run([{'tools': [{'name': 'get_server_time'}]}, {'text': 'Done'}], allow_step_questions=True)
    assert 'Done' in answer and answer.step_extensions == 1
    question.assert_awaited_once_with(1)
    assert len(rig.links) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('ending', [{'exit': True}, {'malformed': True}])
async def test_early_exit_and_malformed_are_fixed_unknown_failures(rig, ending):
    answer = await rig.run([ending])
    assert answer.stopped_reason == 'model_stopped'
    assert 'unrecognized reason' in answer


@pytest.mark.parametrize('event,code', [
    ({'error': {'type': 'rate_limit_error', 'retry_after': 2}}, 'model_rate_limited'),
    ({'error': {'code': 'usage_limit_reached', 'resets_at': 2000000000}}, 'model_usage_limit'),
    ({'result': 'Prompt is too long'}, 'model_context_limit'),
    ({'error': {'type': 'overloaded_error'}}, 'model_provider_error'),
    ({'result': 'Not logged in. Please run /login'}, 'model_auth_rejected'),
    ({'error': {'code': 'unknown_provider_code'}}, 'model_stopped'),
])
def test_fixed_failure_vocabulary(event, code):
    value = failure(event)
    assert value.code == code
    if code == 'model_auth_rejected': assert value.sentence == 'Sign in to Claude again'
    if code == 'model_usage_limit': assert 'UTC' in value.sentence


@pytest.mark.asyncio
async def test_no_retry_after_tool_ran(rig, monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr('core.execution.provider_retry.sleep', sleep)
    answer = await rig.run([{'tools': [{'name': 'get_server_time'}]},
        {'failure': {'error': {'type': 'overloaded_error'}}}])
    assert answer.stopped_reason == 'model_provider_error'
    assert rig.effects == ['time'] and len(rig.links) == 1
    sleep.assert_not_awaited()


@pytest.mark.asyncio
async def test_late_and_repeated_requests_are_refused(rig):
    await rig.run([{'text': 'Done'}])
    turn = rig.turns[0]
    await turn.rpc({'id': 'late', 'method': 'tools/call', 'params': {'name': 'get_server_time'}})
    assert rig.links[0].responses[-1]['error']['message'] == 'turn_ended'
    assert any(kind == 'bridge_refused' for _, kind, _ in rig.records)
    assert not rig.effects


@pytest.mark.asyncio
async def test_model_change_is_hard_failure(rig):
    from core.execution.models import ExecutionError
    with pytest.raises(ExecutionError) as caught:
        await rig.run([{'text': 'Started'}, {'model': 'claude-other-full'}])
    assert caught.value.code == 'binding_mismatch' and 'changed' in caught.value.detail


@pytest.mark.asyncio
async def test_safe_retry_before_any_tool_gets_fresh_invocation(monkeypatch):
    from core.execution.provider_retry import model_call
    monkeypatch.setattr('core.lane_config.require_backend', lambda *a: None)
    monkeypatch.setattr('core.execution.provider_retry.sleep', AsyncMock())
    links = []
    async def connector():
        ops = ([{'failure': {'error': {'type': 'overloaded_error'}}}]
               if not links else [{'text': 'Recovered'}])
        link = FakeLink(ops); links.append(link)
        return await link.connect()
    turn = ClaudeTurn(model='sonnet', connector=connector, recorder=lambda *a: None)
    try:
        response = await model_call(lambda: turn.ainvoke([HumanMessage(content='Hello')]))
        assert response.content == 'Recovered' and len(links) == 2
    finally:
        await turn.close()


@pytest.mark.asyncio
async def test_step_refusal_preserves_receipt_and_ends_cli(rig, monkeypatch):
    from core.faces.registry import get_default_registry
    monkeypatch.setattr(get_default_registry().get_face('assistant-tools'), 'tool_loop_max_rounds', 1)
    monkeypatch.setattr('core.tools.ask.can_request_step_extension', lambda: True)
    monkeypatch.setattr('core.tools.ask.request_step_extension', AsyncMock(return_value=False))
    reply = await rig.run([{'tools': [{'name': 'get_server_time'}]}, {'text': 'Never'}], allow_step_questions=True)
    assert reply.stopped_reason == 'tool_round_limit' and rig.links[0].stopped
    assert rig.effects == ['time']
    assert len([p for _, kind, p in rig.records if kind == 'tool_result']) == 1


@pytest.mark.asyncio
async def test_multi_tool_message_uses_one_round_at_the_boundary(rig, monkeypatch):
    from core.faces.registry import get_default_registry
    monkeypatch.setattr(get_default_registry().get_face('assistant-tools'), 'tool_loop_max_rounds', 1)
    reply = await rig.run([{'tools': [{'name': 'get_server_time'}, {'name': 'get_server_time'}]}, {'text': 'Never'}])
    assert reply.stopped_reason == 'tool_round_limit' and rig.effects == ['time', 'time']
    assert len(rig.links) == 1


@pytest.mark.asyncio
async def test_cli_death_cancels_a_pending_approval_before_any_late_effect(rig, monkeypatch):
    entered, approve = asyncio.Event(), asyncio.Event()
    effects = []
    @tool
    async def get_server_time() -> str:
        """Stand in the real loop while an approval is pending."""
        entered.set()
        await approve.wait()
        effects.append('released')
        return 'done'
    monkeypatch.setattr(execute, 'get_all_tools', AsyncMock(return_value=[get_server_time]))
    task = asyncio.create_task(rig.run([{'tools': [{'name': 'get_server_time'}]}, {'text': 'Never'}]))
    await asyncio.wait_for(entered.wait(), 2)
    rig.links[0].reader.feed_data((json.dumps({'type': 'terminal', 'returncode': 1,
        'cleanup': 'idle_proved'})+'\n').encode())
    with pytest.raises(ClaudeTurnError): await asyncio.wait_for(task, 2)
    approve.set()
    await asyncio.sleep(0)
    assert not effects and rig.turns[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize('problem,code', [
    ({'error': {'type': 'rate_limit_error'}}, 'model_rate_limited'),
    ({'error': {'code': 'usage_limit_reached', 'resets_at': 2000000000}}, 'model_usage_limit'),
    ({'result': 'Prompt is too long'}, 'model_context_limit'),
    ({'error': {'type': 'api_error'}}, 'model_provider_error'),
    ({'result': 'Not logged in. Please run /login'}, 'model_auth_rejected'),
    ({'error': {'code': 'unknown'}}, 'model_stopped'),
])
async def test_each_fixed_failure_through_real_adapter(rig, monkeypatch, problem, code):
    monkeypatch.setattr('core.execution.provider_retry.sleep', AsyncMock())
    reply = await rig.run([{'failure': problem}])
    assert reply.stopped_reason == code
    if code == 'model_auth_rejected':
        assert 'Sign in to Claude again' in reply
        assert any(kind == 'signed_out' for _, kind, _ in rig.records)


@pytest.mark.asyncio
async def test_partial_text_is_saved_once_when_provider_fails(rig):
    reply = await rig.run([{'text': 'Started'}, {'failure': {'error': {'type': 'api_error'}}}])
    assert reply.startswith('Started\n\n') and reply.count('Started') == 1
    assert ''.join(e['content'] for e in rig.frames if e['type'] == 'token') == reply


@pytest.mark.asyncio
async def test_cli_quota_reset_notification_is_retained_on_fixed_failure(rig):
    reply = await rig.run([{'raw': {'type': 'rate_limit_event', 'rate_limit_info': {
        'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': '2026-11-18T12:00:00Z'}}},
        {'failure': {'subtype': 'error_during_execution', 'errors': ["You've hit your limit"]}}])
    assert reply.stopped_reason == 'model_usage_limit'
    assert '2026-11-18 12:00:00 UTC' in reply


@pytest.fixture(autouse=True)
def _lane_configured_by_bob(monkeypatch):
    # The confined Claude path exists only for a lane configured through Bob's
    # own sign-in. This rig takes its test faces from the suite's legacy roster,
    # so it switches the path on directly.
    monkeypatch.setattr('core.backends._lc_openai.claude_turns_enabled', lambda: True)
