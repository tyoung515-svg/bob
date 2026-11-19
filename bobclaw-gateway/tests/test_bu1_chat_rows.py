"""Stored row identity and correlation for ordinary and split streamed turns."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from routers import chat
from tests.fake_pool import InMemoryPostgresPool


@pytest.mark.parametrize('questions', [0, 1, 2])
@pytest.mark.parametrize('request_id', [None, 'desktop-turn-1'])
async def test_completion_identifies_final_row_and_all_earlier_parts(monkeypatch, questions, request_id):
    pool = InMemoryPostgresPool()
    pool.add_conversation(conv_id='conversation', user_id='owner', title='Test')
    frames = []
    for i in range(questions):
        frames += [{'type': 'chunk', 'content': f'Progress {i}.\n\n'},
                   {'type': 'question', 'question_id': f'q{i}', 'question': 'Choose?'}]
    frames += [{'type': 'chunk', 'content': 'Final answer.'},
               {'type': 'message_complete', 'message_id': 'core-transient-id',
                'tokens_in': 3, 'tokens_out': 7, 'elapsed_ms': 12}]
    sent = []

    class Context:
        def __init__(self, value): self.value = value
        async def __aenter__(self): return self.value
        async def __aexit__(self, *args): return False

    async def content():
        for frame in frames:
            yield ('data: ' + json.dumps(frame) + '\n').encode()

    def post(url, *, json):
        sent.append(json)
        return Context(SimpleNamespace(status=200, content=content()))

    monkeypatch.setattr(chat.aiohttp, 'ClientSession', lambda **kw: Context(SimpleNamespace(post=post)))
    monkeypatch.setattr('routers.execution.chat_work_fields', AsyncMock(return_value={}))
    monkeypatch.setattr('routers.execution.chat_pc_fields', AsyncMock(return_value={}))
    ws = SimpleNamespace(send_json=AsyncMock())
    await chat._stream_chat_to_client(ws, 'conversation',
        {'content': 'Hello', **({'request_id': request_id} if request_id else {})}, {}, pool, 'owner')
    rows = pool.messages
    assert [r['content'] for r in rows] == [f'Progress {i}.\n\n' for i in range(questions)] + ['Final answer.']
    for row in rows:
        metadata = json.loads(row['metadata']) if isinstance(row['metadata'], str) else row['metadata']
        assert metadata.get('request_id') == request_id
    assert sent[0].get('request_id') == request_id
    complete = ws.send_json.call_args_list[-1].args[0]
    assert complete['message_id'] == str(rows[-1]['id'])
    assert complete['message_id'] != 'core-transient-id'
    assert complete.get('pre_question_message_ids', []) == [str(r['id']) for r in rows[:-1]]
    assert complete.get('request_id') == request_id
    assert (complete['tokens_in'], complete['tokens_out'], complete['elapsed_ms']) == (3, 7, 12)


async def test_round_stop_is_stored_and_forwarded(monkeypatch):
    pool = InMemoryPostgresPool()
    pool.add_conversation(conv_id='conversation', user_id='owner', title='Test')

    class Context:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): return False
        status = 200
        def post(self, *args, **kw): return self
        @property
        def content(self):
            async def stream():
                yield b'{"type":"chunk","content":"Reply continue to carry on."}\n'
                yield b'{"type":"message_complete","stopped_reason":"tool_round_limit"}\n'
            return stream()

    monkeypatch.setattr(chat.aiohttp, 'ClientSession', lambda **kw: Context())
    monkeypatch.setattr('routers.execution.chat_work_fields', AsyncMock(return_value={}))
    monkeypatch.setattr('routers.execution.chat_pc_fields', AsyncMock(return_value={}))
    ws = SimpleNamespace(send_json=AsyncMock())
    await chat._stream_chat_to_client(ws, 'conversation', {'content': 'Hello', 'request_id': 'turn'}, {}, pool, 'owner')
    metadata = pool.messages[-1]['metadata']
    if isinstance(metadata, str): metadata = json.loads(metadata)
    assert metadata['stopped_reason'] == 'tool_round_limit'
    assert metadata['request_id'] == 'turn'
    assert ws.send_json.call_args.args[0]['stopped_reason'] == 'tool_round_limit'
