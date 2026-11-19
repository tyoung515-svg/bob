"""Question correlation uses the stored question, never operator-supplied metadata."""
from contextlib import asynccontextmanager
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.execution import chat_records


@pytest.mark.asyncio
@pytest.mark.parametrize('turn', [None, 'turn-one', 'turn-two'])
async def test_question_and_answer_keep_turn_id_and_replay_once(monkeypatch, turn):
    conversation = uuid.uuid4()

    class Pool:
        def __init__(self): self.questions = {}; self.messages = []
        @asynccontextmanager
        async def acquire(self): yield self
        @asynccontextmanager
        async def transaction(self): yield
        async def fetchval(self, sql, *args): return conversation
        async def fetchrow(self, sql, qid, owner):
            row = self.questions.get(qid)
            return row if row and row['user_id'] == owner else None
        async def execute(self, sql, *args):
            if sql.startswith('INSERT INTO chat_questions'):
                qid, conv, owner, text, choices, expires, request_id, _origin = args
                self.questions[qid] = dict(id=qid, conversation_id=conv, user_id=owner,
                    question=text, expires_at=expires, answer=None, request_id=request_id)
            elif sql.startswith('INSERT INTO messages'):
                self.messages.append(dict(id=args[0], content=args[2], metadata=json.loads(args[3])))
            elif sql.startswith('UPDATE chat_questions'):
                self.questions[args[0]]['answer'] = args[1]
            else: raise AssertionError(sql)

    pool = Pool()
    monkeypatch.setattr(chat_records, 'POOL', pool)
    monkeypatch.setattr('core.tools.ask.pending_questions', lambda: ['q1', 'q2'])
    delivered = []
    monkeypatch.setattr('core.tools.ask.answer_question', lambda q, a: delivered.append((q, a)) or True)
    token = chat_records.request_id.set(turn)
    try:
        for qid in ('q1', 'q2'):
            assert await chat_records.question(qid, 'owner', str(conversation), 'Choose?', [], time.time()+60)
    finally:
        chat_records.request_id.reset(token)
    assert chat_records.request_id.get() is None
    for qid in ('q1', 'q2'):
        value = dict(question_id=qid, user_id='owner', answer='Yes')
        first = await chat_records.accept(value)
        assert first['delivered_to_turn']
        assert (await chat_records.accept(value))['replayed']
    assert len(pool.messages) == 2
    assert delivered == [('q1', 'Yes'), ('q2', 'Yes')]
    for row, qid in zip(pool.messages, ('q1', 'q2')):
        assert row['metadata'] == {'kind': 'operator_answer', 'question_id': qid,
            **({'request_id': turn} if turn else {})}


@pytest.mark.asyncio
async def test_desktop_bindings_take_identity_only_from_the_shipped_provider_preset(monkeypatch):
    # BU8: a shipped preset for a provider's own endpoint declares the identity
    # and family of the model it asks for. Anything else (another model on that
    # backend, or the same model name on a local backend) stays unknown, so a
    # name alone can never count as an independent reviewer.
    from core.lane_config import agent_service_definitions
    from core.execution.dispatcher import exact_models
    from core.execution.workflows import WorkflowRuntime
    definitions = agent_service_definitions([
        dict(backend='deepseek_v4_flash', models=['deepseek-v4-flash', 'some-other-model'], available=True),
        dict(backend='local', models=['deepseek-v4-flash'], available=True)])
    models = exact_models(definitions['models'])
    monkeypatch.setattr('core.nodes.execute._is_tool_enabled', lambda *args: True)
    bindings = await WorkflowRuntime(SimpleNamespace(models=AsyncMock(return_value=models))).bindings(None)
    by_key = {(b['backend'], b['model']): b for b in bindings}
    preset = by_key[('deepseek_v4_flash', 'deepseek-v4-flash')]
    assert preset['family'] == 'deepseek'
    assert preset['model_identity'] == 'deepseek/deepseek-v4-flash'
    assert 'canonical_model_identity_unavailable' not in preset['reasons']
    for key in (('deepseek_v4_flash', 'some-other-model'), ('local', 'deepseek-v4-flash')):
        assert by_key[key]['family'] is None
        assert by_key[key]['model_identity'] == 'unknown'
        assert 'canonical_model_identity_unavailable' in by_key[key]['reasons']


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', [None, 'tool_round_limit', 'model_rate_limited', 'model_provider_error'])
async def test_core_ingress_and_completion_carry_turn_metadata(monkeypatch, reason):
    from aiohttp.test_utils import TestClient, TestServer
    from api.server import build_app
    monkeypatch.setattr(chat_records, 'POOL', None)
    inputs = []
    class Graph:
        async def astream(self, graph_input, config, stream_mode):
            inputs.append(graph_input)
            yield 'custom', {'type': 'token', 'content': 'Visible progress.'}
            yield 'updates', {'execute': {'messages': [{'role': 'assistant', 'content': 'Visible progress.'}],
                **({'stopped_reason': reason} if reason else {})}}
    async with TestClient(TestServer(build_app(graph=Graph()))) as client:
        response = await client.post('/api/chat', json=dict(conversation_id='fixture',
            content='Compare settings', face_id='assistant', request_id='core-turn'))
        assert response.status == 200
        frames = [json.loads(line[5:]) for line in (await response.text()).splitlines() if line.startswith('data:')]
    assert inputs[0]['request_id'] == 'core-turn'
    assert inputs[0]['stopped_reason'] is None
    assert ''.join(f['content'] for f in frames if f['type'] == 'chunk') == 'Visible progress.'
    complete = frames[-1]
    assert complete['type'] == 'message_complete' and complete.get('stopped_reason') == reason
