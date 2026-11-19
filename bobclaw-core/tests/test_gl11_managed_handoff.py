"""GL11: actual tool loop with fake models/receipts; no provider or worker IO."""
import ast
import asyncio
import json
import logging
import sys
from contextvars import ContextVar
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from core.builder.service import Builder
from core.execution.models import Authority, ExecutionError
from core.tools import ask, builder_tools


@pytest.fixture(autouse=True)
def mock_redis():
    # Like test_builder_session: the loop AST runs without optional provider SDKs.
    yield


@pytest.fixture
def loop(monkeypatch):
    import core.faces.registry as registry_module
    import core.faces.prompt as prompt_module
    import core.context as context_module
    registry = SimpleNamespace(get_allowed_tools=lambda face: [], list_skills=lambda face: [],
                               get_face=lambda face: SimpleNamespace(tool_loop_max_rounds=3))
    monkeypatch.setattr(registry_module, 'get_default_registry', lambda: registry)
    monkeypatch.setattr(prompt_module, 'interactive_messages', lambda messages, *args: messages)
    monkeypatch.setattr(context_module, 'enforce_backend_limits', lambda messages, *args: messages)
    monkeypatch.setitem(sys.modules, 'core.tools.registry', SimpleNamespace(_skill_face_id=ContextVar('gl11-face')))
    source = (Path(__file__).parents[1]/'core/nodes/execute.py').read_text(encoding='utf-8')
    names = {'_finish_tool_reply', '_run_tool_loop'}
    names.update({'ToolLoopReply', 'ToolRoundLimitReply'})
    nodes = [n for n in ast.parse(source).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in names]
    namespace = dict(json=json, uuid=uuid, logger=logging.getLogger(__name__), perf_counter=perf_counter,
                     ToolMessage=ToolMessage, config=SimpleNamespace(TOOL_LOOP_MAX_ROUNDS=3),
                     check_tool_access=lambda *args: True, require_backend=lambda *args: None,
                     _dict_messages_to_lc=lambda messages: messages,
                     _emit_tool_activity=lambda *args, **kwargs: None, _tool_preview=str)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual GL11 tool loop>', 'exec'), namespace)

    def setup(responses, tools, rounds=3):
        calls = []
        class Model:
            def bind_tools(self, bound):
                return self
            async def ainvoke(self, messages):
                calls.append(list(messages))
                return responses[len(calls)-1]
        registry.get_face = lambda face: SimpleNamespace(tool_loop_max_rounds=rounds)
        namespace.update(_build_tool_model=lambda backend: Model(), get_all_tools=AsyncMock(return_value=tools))
        return namespace['_run_tool_loop'], calls
    return setup


def call(name, args=None, content=''):
    return AIMessage(content=content, tool_calls=[{'name': name, 'args': args or {}, 'id': uuid.uuid4().hex}])


@pytest.mark.parametrize('face', ['assistant', 'planner'])
@pytest.mark.parametrize('earlier_round', [False, True])
async def test_plan_streams_before_real_question_and_is_kept_in_reply(loop, earlier_round, face):
    @tool
    async def inspect() -> str:
        """Inspect supplied evidence."""
        return 'inspected'
    plan = [{'type': 'reasoning', 'text': 'private'}, {'type': 'output_text', 'text': 'Plan: a daily planner.'}]
    responses = ([call('inspect', content=plan)] if earlier_round else []) + [
        call('ask_operator', {'question': 'Approve this plan?', 'choices': ['Yes', 'Adjust']},
             content='' if earlier_round else plan), AIMessage(content='Ready for your click.')]
    run, calls = loop(responses, [inspect, ask.ask_operator])
    frames = []
    def writer(frame):
        frames.append(frame)
        if frame['type'] == 'question':
            assert ''.join(e['content'] for e in frames if e['type'] == 'token') == 'Plan: a daily planner.\n\n'
            assert ask.answer_question(frame['question_id'], 'Yes')
    reply = await asyncio.wait_for(run([], 'fixture', face, writer), 2)
    assert reply == 'Plan: a daily planner.\n\nReady for your click.'
    assert ''.join(e['content'] for e in frames if e['type'] == 'token') == reply
    assert 'private' not in reply and len(calls) == len(responses)
    assert not ask.pending_questions()


@pytest.mark.parametrize('report', ['Built Dayline in index.html. Parsed HTML with a parser. Clicks remain unverified.', None])
async def test_finish_uses_report_or_honest_fallback_without_another_round(loop, report):
    @tool
    async def task_finish() -> str:
        """Return the supplied terminal receipt."""
        return json.dumps({'end_turn': True, 'status': 'completed', 'report': report})
    run, calls = loop([call('task_finish')], [task_finish])
    frames = []
    reply = await run([], 'fixture', 'builder', frames.append)
    assert reply == (report or 'The Builder finished without writing a report.\ntask_finish: completed; ')
    assert len(calls) == 1 and frames[-1]['content'] == reply


async def test_round_limit_does_not_repeat_pre_tool_text_as_a_final_report(loop):
    @tool
    async def inspect() -> str:
        """Return a check result."""
        return '{"exit_code":1}'
    run, calls = loop([call('inspect', content='I will check it.')], [inspect], rounds=1)
    frames = []
    reply = await run([], 'fixture', 'builder', frames.append)
    assert reply.count('I will check it.') == 1
    assert 'before finishing' in reply and '"continue"' in reply
    assert reply.stopped_reason == 'tool_round_limit'
    assert 'exit_code' not in reply and 'Last results' not in reply
    assert ''.join(e['content'] for e in frames if e['type'] == 'token') == reply


async def test_terminal_error_keeps_fallback_and_does_not_use_untrusted_report(loop):
    @tool
    async def inspect() -> str:
        """Return a terminal expiry."""
        return json.dumps({'end_turn': True, 'error': {'code': 'approval_expired'}, 'report': 'Everything passed.'})
    run, calls = loop([call('inspect')], [inspect])
    reply = await run([], 'fixture', 'builder', None)
    assert 'approval expired' in reply and 'Everything passed' not in reply and len(calls) == 1


async def test_task_finish_requires_and_forwards_model_report(monkeypatch):
    assert builder_tools.task_finish.args_schema.model_json_schema()['required'] == ['report']
    invoke = AsyncMock(return_value='receipt')
    monkeypatch.setattr(builder_tools, 'invoke', invoke)
    assert await builder_tools.task_finish.ainvoke({'report': 'Built it; checks remain unverified.'}) == 'receipt'
    invoke.assert_awaited_once_with('task_finish', {'report': 'Built it; checks remain unverified.'})


async def test_finish_closes_allowance_and_returns_report(monkeypatch):
    builder = Builder(None)
    conversation = uuid.uuid4()
    builder.store.mode = AsyncMock(return_value={'conversation_id': conversation})
    end = AsyncMock()
    monkeypatch.setattr('core.builder.lifecycle.end_sessions', end)
    auth = Authority('lane', 'owner', 'owner', {}, 'portal')
    report = 'Built index.html in Dayline. Parser passed; browser behavior unverified.'
    result = await builder.tool(auth, str(conversation), 'task_finish', {'report': report})
    assert result['report'] == report and result['end_turn'] is True
    end.assert_awaited_once_with(None, 'lane', 'owner', [conversation], reason='task_completed')


@pytest.mark.parametrize('report', ['', 'x'*6001, 12])
async def test_invalid_finish_report_is_refused_before_closing_allowance(monkeypatch, report):
    builder = Builder(None)
    builder.store.mode = AsyncMock(return_value={'conversation_id': uuid.uuid4()})
    end = AsyncMock()
    monkeypatch.setattr('core.builder.lifecycle.end_sessions', end)
    with pytest.raises(ExecutionError, match='invalid_task_report'):
        await builder.tool(Authority('lane', 'owner', 'owner', {}, 'portal'), 'conversation', 'task_finish', {'report': report})
    end.assert_not_awaited()


async def test_owned_project_without_registry_root_points_to_create_without_approval_or_dispatch(monkeypatch):
    project = uuid.uuid4()
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value={'id': project, 'name': 'Daily Planner Test'}), execute=AsyncMock())
    builder = Builder(pool)
    builder.store.mode = AsyncMock(return_value={'project_id': project})
    builder.worker = AsyncMock()
    monkeypatch.setattr(builder, 'workspace', lambda *args: None)
    result = await builder.tool(Authority('lane', 'owner', 'owner', {}, 'portal'), 'conversation',
                                'workspace_mount', {'project_id': str(project)})
    assert result['error']['code'] == 'workspace_unavailable'
    assert result['tools'] == ['workspace_create']
    assert 'no registered workspace' in result['detail'] and 'Do not retry' in result['detail']
    builder.worker.assert_not_awaited()
    pool.execute.assert_not_awaited()


async def test_oversized_question_is_rejected_instead_of_truncating_plan():
    raw = await ask.ask_operator.ainvoke({'question': 'x'*200})
    assert 'under 200 characters' in json.loads(raw)['error']
    assert not ask.pending_questions()
