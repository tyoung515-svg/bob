"""Platform step questions require committed owner answers; no model extension tool."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from core.config import config
from core.execution import chat_records
from core.nodes import execute
from core.tools import ask
from test_tool_loop import _FakeModel, _tool_call_response, _final_response


class QuestionPool:
    """Question/answer SQL fake: accept() commits before it resolves the future."""
    def __init__(self):
        self.questions = {}
        self.answers = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield self

    async def fetchval(self, sql, *args, **kwargs):
        if sql.startswith('SELECT id FROM conversations'):
            return args[0]
        assert "q.origin='step_limit'" in sql
        row = self.questions[args[0]]
        assert (row['user_id'], row['conversation_id'], row['request_id']) == args[1:]
        return 1 if row['answer'] == 'Keep going' and row['answered_at'] else None

    async def fetchrow(self, sql, qid, user):
        row = self.questions.get(qid)
        return row if row and row['user_id'] == user else None

    async def execute(self, sql, *args):
        if sql.startswith('INSERT INTO chat_questions'):
            qid, conv, owner, text, choices, expires, request, origin = args
            self.questions[qid] = dict(id=qid, conversation_id=conv, user_id=owner,
                question=text, choices=json.loads(choices), expires_at=expires,
                request_id=request, origin=origin, answer=None, answered_at=None)
        elif sql.startswith('INSERT INTO messages'):
            self.answers.append(dict(content=args[2], metadata=json.loads(args[3])))
        elif sql.startswith('UPDATE chat_questions'):
            self.questions[args[0]].update(answer=args[1], answered_at=True)
        else:
            raise AssertionError(sql)


@pytest.fixture
def step_turn(monkeypatch):
    monkeypatch.setattr(config, 'TOOL_LOOP_MAX_ROUNDS', 1)
    monkeypatch.setattr(execute, '_append_agent_turn_event', AsyncMock())
    monkeypatch.setattr(ask, 'DEFAULT_TIMEOUT_S', 0.02)
    pool = QuestionPool()
    monkeypatch.setattr(chat_records, 'POOL', pool)
    frames, answers, tasks = [], [], []

    def writer(frame):
        frames.append(frame)
        if frame['type'] == 'question':
            assert frame['question_id'] in pool.questions  # stored BEFORE publication
            assert frame['origin'] == 'step_limit'
            if answers:
                answer = answers.pop(0)
                if answer == 'cancel':
                    asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
                elif answer == 'unstored':
                    ask.answer_question(frame['question_id'], 'Keep going')
                else:
                    tasks.append(asyncio.create_task(chat_records.accept(dict(
                        question_id=frame['question_id'], user_id='owner', answer=answer))))

    monkeypatch.setattr(execute, '_get_stream_writer', lambda: writer)
    state = dict(task='Keep checking time.', backend='deepseek_v4_flash', face_id='assistant-tools',
                 messages=[], user_id='owner', conversation_id=str(uuid.uuid4()), request_id='bu2-turn',
                 step_questions_allowed=True)
    yield SimpleNamespace(pool=pool, frames=frames, answers=answers, tasks=tasks, state=state)
    ask.reset_pending_questions()


async def run(turn, monkeypatch, *, final_after=None):
    responses = [_tool_call_response('get_server_time', f'call-{i}') for i in range(final_after or 10)]
    if final_after:
        responses.append(_final_response('Finished.'))
    model = _FakeModel(responses)
    monkeypatch.setattr(execute, '_build_tool_model', lambda backend: model)
    result = await execute.execute_node(turn.state)
    if turn.tasks:
        await asyncio.gather(*turn.tasks)
    return result, model.bound_model.invocations


async def test_keep_going_continues_and_records_count_and_origin(step_turn, monkeypatch):
    step_turn.answers.append('Keep going')
    result, calls = await run(step_turn, monkeypatch, final_after=1)
    assert len(calls) == 2 and result['step_extensions'] == 1
    assert not result.get('stopped_reason') and result['messages'][-1]['content'].endswith('Finished.')
    questions = [f for f in step_turn.frames if f['type'] == 'question']
    assert len(questions) == 1 and '1 steps' in questions[0]['question']
    assert questions[0]['choices'] == ['Keep going', 'Stop here']
    metadata = step_turn.pool.answers[0]['metadata']
    assert metadata['origin'] == 'step_limit' and metadata['request_id'] == 'bu2-turn'
    assert all(r['origin'] == 'step_limit' for r in step_turn.pool.questions.values())


@pytest.mark.parametrize('answer', ['Stop here', None, 'cancel', 'unstored'])
async def test_stop_timeout_cancellation_and_unstored_answer_cannot_extend(step_turn, monkeypatch, answer):
    if answer:
        step_turn.answers.append(answer)
    result, calls = await run(step_turn, monkeypatch)
    assert len(calls) == 1 and result['step_extensions'] == 0
    assert result['stopped_reason'] == 'tool_round_limit'
    assert 'before finishing' in result['messages'][-1]['content']
    assert len(step_turn.pool.questions) == 1 and ask.pending_questions() == []


async def test_three_extensions_and_no_fourth_question(step_turn, monkeypatch):
    step_turn.answers[:] = ['Keep going'] * 4
    result, calls = await run(step_turn, monkeypatch)
    assert len(calls) == 4 and len(step_turn.pool.questions) == 3
    assert result['step_extensions'] == 3 and result['stopped_reason'] == 'tool_round_limit'


async def test_old_client_without_request_id_can_still_answer_platform_question(step_turn, monkeypatch):
    step_turn.state.pop('request_id')
    step_turn.answers.append('Keep going')
    result, calls = await run(step_turn, monkeypatch, final_after=1)
    frame = next(f for f in step_turn.frames if f['type'] == 'question')
    assert frame['request_id'] and result['step_extensions'] == 1
    assert step_turn.pool.answers[0]['metadata']['request_id'] == frame['request_id']


async def test_without_writer_keeps_existing_stop(step_turn, monkeypatch):
    monkeypatch.setattr(execute, '_get_stream_writer', lambda: None)
    result, calls = await run(step_turn, monkeypatch)
    assert len(calls) == 1 and not step_turn.pool.questions
    assert result['stopped_reason'] == 'tool_round_limit' and result['step_extensions'] == 0


async def test_workflow_round_limit_never_asks(step_turn, monkeypatch):
    from core.execution.workflow_context import current
    from core.execution.models import ExecutionError
    model = _FakeModel([_tool_call_response('get_server_time', 'one')])
    monkeypatch.setattr(execute, '_build_tool_model', lambda backend: model)
    workflow = SimpleNamespace(summary=False, round_limit=1, messages=lambda messages, tools: messages,
        model_call=lambda model, messages: model.ainvoke(messages),
        before_tool=AsyncMock(), tool_result=AsyncMock())
    token = current.set(workflow)
    try:
        with pytest.raises(ExecutionError, match='tool_round_limit'):
            await execute._run_tool_loop([], 'deepseek_v4_flash', 'assistant-tools', step_turn.frames.append)
    finally:
        current.reset(token)
    assert not step_turn.pool.questions


async def test_mcp_service_turn_does_not_enter_extendable_loop(step_turn, monkeypatch):
    monkeypatch.setattr(execute, '_run_tool_loop', AsyncMock(side_effect=AssertionError('must not run')))
    monkeypatch.setattr(execute, '_stream_and_collect', AsyncMock(return_value='Service reply.'))
    result = await execute.execute_node({**step_turn.state, 'agent_service_single_turn': True})
    assert result.get('error') is None and not step_turn.pool.questions


def test_question_tool_schema_cannot_claim_platform_origin():
    assert 'origin' not in ask.ask_operator.args
    assert 'step_extensions' not in ask.ask_operator.args
