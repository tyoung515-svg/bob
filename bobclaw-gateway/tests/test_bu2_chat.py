"""BU2 question origin, split history, extension count and broker clock contract."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routers import chat
from tests.fake_pool import InMemoryPostgresPool
from tests.test_gl9_turn_budget import make_job


async def test_retired_memory_filter_is_forwarded_without_a_new_surface(monkeypatch):
    from routers import memory
    proxy = AsyncMock(return_value=(200, '[]'))
    monkeypatch.setattr(memory, '_proxy', proxy)
    response = await memory.list_memory_facts(SimpleNamespace(query={'status': 'retired', 'limit': '10'}))
    assert response.status == 200
    proxy.assert_awaited_once_with('GET', '/api/memory/facts', {'status': 'retired', 'limit': '10'})
@pytest.mark.parametrize('token_type,allowed', [('agent', False), ('portal', True), (None, True)])
@pytest.mark.parametrize('request_id', ['turn', None])
async def test_step_question_metadata_and_eligibility(monkeypatch, token_type, allowed, request_id):
    pool = InMemoryPostgresPool()
    pool.add_conversation(conv_id='conversation', user_id='owner', title='Test')
    payloads = []
    correlation = request_id or 'platform-turn'

    class Context:
        def __init__(self, value): self.value = value
        async def __aenter__(self): return self.value
        async def __aexit__(self, *args): return False

    async def content():
        frames = [{'type': 'chunk', 'content': 'I have used 50 steps and I am not finished.\n\n'},
                  {'type': 'question', 'question_id': 'q1', 'question': 'Keep going?',
                   'origin': 'step_limit', 'request_id': correlation, 'choices': ['Keep going', 'Stop here']},
                  {'type': 'chunk', 'content': 'Finished.'},
                  {'type': 'message_complete', 'step_extensions': 1}]
        for frame in frames:
            yield ('data: ' + json.dumps(frame) + '\n').encode()
            if frame['type'] == 'question':
                # The core commits the owner's answer while the gateway waits.
                await chat._save_message(pool, 'conversation', 'user', 'Operator answer to: Keep going?\nKeep going',
                    {'kind': 'operator_answer', 'question_id': 'q1', 'origin': 'step_limit', 'request_id': correlation})

    def post(url, *, json):
        payloads.append(json)
        return Context(SimpleNamespace(status=200, content=content()))

    monkeypatch.setattr(chat.aiohttp, 'ClientSession', lambda **kw: Context(SimpleNamespace(post=post)))
    monkeypatch.setattr('routers.execution.chat_work_fields', AsyncMock(return_value={}))
    monkeypatch.setattr('routers.execution.chat_pc_fields', AsyncMock(return_value={}))
    claims = {'sub': 'owner', **({'token_type': token_type} if token_type else {})}
    ws = SimpleNamespace(send_json=AsyncMock())
    await chat._stream_chat_to_client(ws, 'conversation',
        {'content': 'Continue the work.', 'request_id': request_id, 'step_questions_allowed': True}, {}, pool, 'owner',
        token_claims=claims)
    assert payloads[0]['step_questions_allowed'] is allowed
    assert [r['role'] for r in pool.messages] == ['assistant', 'user', 'assistant']
    metadata = [json.loads(r['metadata']) if isinstance(r['metadata'], str) else r['metadata'] for r in pool.messages]
    assert all(m['request_id'] == correlation for m in metadata)
    assert metadata[0]['origin'] == metadata[1]['origin'] == 'step_limit'
    assert metadata[2]['step_extensions'] == 1
    frames = [c.args[0] for c in ws.send_json.call_args_list]
    assert next(f for f in frames if f['type'] == 'question')['origin'] == 'step_limit'
    assert frames[-1]['step_extensions'] == 1
    assert frames[-1]['request_id'] == correlation


@pytest.mark.parametrize('extensions', [0, 3])
def test_broker_forwards_origin_and_count_and_resumes_question_clock(monkeypatch, extensions):
    _, job, events, _ = make_job(monkeypatch, [
        (70, {'type': 'question', 'question_id': 'q1', 'origin': 'step_limit'}),
        (78, {'type': 'tool_activity', 'tool': 'ask_operator', 'phase': 'result'}),
        (200, {'type': 'message_complete', 'step_extensions': extensions}),
    ])
    job._read_loop()
    assert next(v for name, v in events if name == 'question')['origin'] == 'step_limit'
    assert events[-1][1]['step_extensions'] == extensions
    assert job.question_until is None and job.deadline == 378
