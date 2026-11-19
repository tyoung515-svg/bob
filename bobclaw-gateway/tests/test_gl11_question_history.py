"""Pre-question prose is durable before the operator can answer; no external IO."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routers import chat
from tests.fake_pool import InMemoryPostgresPool


@pytest.mark.parametrize('face', ['assistant', 'planner'])
@pytest.mark.parametrize('stop_at_question', [False, True])
async def test_question_flushes_prose_before_answer_and_final_does_not_repeat_it(monkeypatch, stop_at_question, face):
    pool = InMemoryPostgresPool()
    pool.add_conversation(title='Planner', conv_id='conversation', user_id='owner')
    frames = []
    questions = [
        {'type': 'question', 'question_id': 'one', 'question': 'Which layout?', 'choices': ['Timeline', 'List']},
        {'type': 'question', 'question_id': 'two', 'question': 'Approve this plan?', 'choices': ['Yes', 'Adjust']},
    ]

    async def send(frame):
        frames.append(frame)
        if frame['type'] == 'question':
            # The browser must not see a question before preceding prose is saved.
            last = pool.messages[-1]
            expected = 'Plan: a daily planner.\n\n' if frame['question_id'] == 'one' else 'Revised plan: timeline.\n\n'
            assert last['role'] == 'assistant' and last['content'] == expected
            assert json.loads(last['metadata']) == {'kind': 'pre_question', 'question_id': frame['question_id']}
            if not stop_at_question:
                # Simulate the core's durable operator-answer row when it resumes.
                await chat._save_message(pool, 'conversation', 'user', 'Answer: Yes', {'question_id': frame['question_id']})

    async def stream():
        events = [{'type': 'chunk', 'content': 'Plan: a daily planner.\n\n'}, questions[0]]
        if not stop_at_question:
            events += [{'type': 'chunk', 'content': 'Revised plan: timeline.\n\n'}, questions[1],
                       {'type': 'chunk', 'content': 'Click Start Builder to begin.'}, {'type': 'message_complete'}]
        for event in events:
            yield ('data: ' + json.dumps(event) + '\n').encode()
        if stop_at_question:
            raise asyncio.CancelledError()

    class Context:
        def __init__(self, value):
            self.value = value
        async def __aenter__(self):
            return self.value
        async def __aexit__(self, *args):
            return False

    response = SimpleNamespace(status=200, content=stream())
    session = SimpleNamespace(post=lambda *a, **kw: Context(response))
    monkeypatch.setattr(chat.aiohttp, 'ClientSession', lambda **kw: Context(session))
    monkeypatch.setattr('routers.execution.chat_work_fields', AsyncMock(return_value={}))
    monkeypatch.setattr('routers.execution.chat_pc_fields', AsyncMock(return_value={}))
    await chat._stream_chat_to_client(SimpleNamespace(send_json=send), 'conversation',
                                     {'content': 'Plan an app'}, {}, pool, user_id='owner', vetted_face_id=face)
    assert frames[0]['type'] == 'chunk' and frames[1] == questions[0]
    assert frames[-1]['type'] == 'message_complete'
    if stop_at_question:
        assert [r['content'] for r in pool.messages] == ['Plan: a daily planner.\n\n']
    else:
        assert [r['role'] for r in pool.messages] == ['assistant', 'user', 'assistant', 'user', 'assistant']
        assert [r['content'] for r in pool.messages if r['role'] == 'assistant'] == [
            'Plan: a daily planner.\n\n', 'Revised plan: timeline.\n\n', 'Click Start Builder to begin.']
    assert ''.join(r['content'] for r in pool.messages if r['role'] == 'assistant') == ''.join(
        frame['content'] for frame in frames if frame['type'] == 'chunk')
