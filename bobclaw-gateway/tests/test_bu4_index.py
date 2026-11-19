"""Stored rows cross the real signed handoff after completion; no live services."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from routers import chat
from tests.fake_pool import InMemoryPostgresPool
from core.context import ingest, store
from core.execution import chat_records
from core import lane_config
from core.tools import conv_tools as ct
from core.tools.memory_tools import memory_tool_user_id, memory_tool_conversation_id


class Pool(InMemoryPostgresPool):
    async def fetchrow(self, query, *args):
        row = await super().fetchrow(query, *args)
        if 'INSERT INTO messages' in query:
            row['id'] = uuid4()
        return row

    async def fetch(self, query, *args, **kwargs):
        if 'FROM messages m JOIN conversations c' in query:
            if 'm.id=ANY($3::uuid[])' in query:
                cid, owner, ids = args
                conv = self.conversations.get(str(cid))
                if not conv or conv['user_id'] != owner or conv['is_archived']:
                    return []
                return [r for r in self.messages if r['conversation_id'] == str(cid) and r['id'] in ids]
            cid, owner, after, mid, limit = args
            assert 'c.user_id=$2' in query and 'c.is_archived=FALSE' in query
            assert kwargs == {'timeout': 5} and limit == ingest.PAGE_SIZE
            conv = self.conversations.get(cid)
            if not conv or conv['user_id'] != owner or conv['is_archived']:
                return []
            rows = sorted((r for r in self.messages if r['conversation_id'] == cid),
                          key=lambda r: (r['created_at'], r['id']))
            return [{k: r[k] for k in ('id', 'role', 'content', 'created_at')}
                    for r in rows if after is None or (r['created_at'], r['id']) > (after, mid)][:limit]
        if 'WHERE user_id=$1 AND id=ANY($2::uuid[]) AND is_archived=FALSE' in query:
            return [c for cid, c in self.conversations.items() if cid in {str(i) for i in args[1]}
                    and c['user_id'] == args[0] and not c['is_archived']]
        return await super().fetch(query, *args)

    async def fetchval(self, query, cid, owner, **kwargs):
        assert 'id=$1 AND user_id=$2 AND is_archived=FALSE' in query
        c = self.conversations.get(str(cid))
        return c['id'] if c and c['user_id'] == owner and not c['is_archived'] else None


class Context:
    def __init__(self, value=None, enter=None): self.value, self.enter = value, enter
    async def __aenter__(self): return await self.enter() if self.enter else self.value
    async def __aexit__(self, *args): return False


@pytest.fixture
def indexed(tmp_path, monkeypatch):
    lane = lane_config.LaneConfiguration(tmp_path, {}, {}, None)
    monkeypatch.setattr(lane_config, 'configured_mode', lambda: True)
    monkeypatch.setattr(lane_config, 'get_lane', lambda: lane)
    monkeypatch.setenv('CONTEXT_DB', str(tmp_path / 'context.db'))
    pool = Pool()
    cid = str(uuid4())
    pool.add_conversation(conv_id=cid, user_id='owner', title='HTML daily planner')
    monkeypatch.setattr(chat_records, 'POOL', pool)
    monkeypatch.setattr('routers.execution.chat_work_fields', AsyncMock(return_value={}))
    monkeypatch.setattr('routers.execution.chat_pc_fields', AsyncMock(return_value={}))
    return pool, cid


@pytest.mark.parametrize('mode', ['normal', 'one_question', 'two_questions', 'stopped',
                                 'step_limit', 'failed', 'http_failure', 'transport_failure'])
async def test_every_stored_row_is_indexed_after_reply(indexed, monkeypatch, mode):
    pool, cid = indexed
    await chat._save_message(pool, cid, 'user', 'Build an HTML daily planner')
    frames, pages = [], []
    relay_returned = False
    questions = {'one_question': 1, 'two_questions': 2}.get(mode, 0)

    async def stream():
        for i in range(questions):
            yield json.dumps({'type': 'chunk', 'content': f'Plan {i}.'}).encode()
            yield json.dumps({'type': 'question', 'question_id': f'q{i}'}).encode()
        if mode == 'transport_failure':
            raise chat.aiohttp.ClientConnectionError('fixture failure')
        if mode == 'failed':
            yield b'{"type":"error","message":"Failed","code":"state_error"}'
        else:
            yield b'{"type":"chunk","content":"Planner answer."}'
        if mode == 'stopped':
            raise asyncio.CancelledError()
        yield json.dumps({'type': 'message_complete', **(
            {'stopped_reason': 'tool_round_limit'} if mode == 'step_limit' else {})}).encode()

    def post(url, *, json):
        if url.endswith('/api/chat/index'):
            async def enter():
                assert relay_returned
                if mode != 'transport_failure':
                    assert frames[-1]['type'] in {'message_complete', 'error'}
                pages.append(json)
                status, _ = await ingest.receive(json, chat.config.BOBCLAW_SECRET)
                assert status == 200
                return SimpleNamespace(status=status)
            return Context(enter=enter)
        return Context(SimpleNamespace(status=503 if mode == 'http_failure' else 200,
                                       text=AsyncMock(return_value='Upstream failed'), content=stream()))

    async def send(frame):
        frames.append(frame)
        if frame['type'] == 'question':
            await chat._save_message(pool, cid, 'user', 'Owner answer ' + frame['question_id'])

    monkeypatch.setattr(chat.aiohttp, 'ClientSession', lambda **kw: Context(SimpleNamespace(post=post)))
    try:
        await chat._stream_chat_to_client(SimpleNamespace(send_json=send), cid,
                                         {'content': 'Build a planner'}, {}, pool, 'owner')
    except chat.aiohttp.ClientConnectionError:
        # Existing transport failure handling still leaves the saved user row.
        pass
    relay_returned = True
    assert not pages  # The reply path never awaits indexing.
    await asyncio.gather(*tuple(chat._INDEX_TASKS))
    rows = store.list_messages(cid, 'owner')
    expected = sorted(pool.messages, key=lambda r: (r['created_at'], r['id']))
    assert [r['message_id'] for r in rows] == [str(r['id']) for r in expected]
    assert [r['content'] for r in rows] == [r['content'] for r in expected]
    assert [r['seq'] for r in rows] == list(range(len(rows)))
    assert [json.loads(r['parents_json']) for r in rows] == [
        [] if i == 0 else [rows[i-1]['message_id']] for i in range(len(rows))]
    assert len(rows) == (1 if mode in {'http_failure', 'transport_failure'} else 2 + questions * 2)

    if mode == 'normal':
        tokens = (memory_tool_user_id.set('owner'), memory_tool_conversation_id.set(str(uuid4())),
                  ct.begin_history_turn())
        try:
            hits = json.loads(await ct.conversations_find.ainvoke({'query': 'HTML planner'}))['hits']
            assert hits[0]['conversation_id'] == cid and hits[0]['message_id'] == rows[0]['message_id']
            opened = await ct.conversations_read.ainvoke({'conversation_id': cid})
            assert rows[0]['message_id'] in opened and 'Planner answer.' in opened
        finally:
            memory_tool_user_id.reset(tokens[0])
            memory_tool_conversation_id.reset(tokens[1])
            ct.history_turn.reset(tokens[2])


async def test_full_history_replay_is_paged_repairs_truncation_and_never_reads_other_owner(indexed, monkeypatch):
    pool, cid = indexed
    for i in range(205):
        await chat._save_message(pool, cid, 'user', f'Original message {i}')
    last = pool.messages[-1]
    store.record_history(cid, 'owner', [dict(id=str(last['id']), role='user', content='truncated')])
    foreign = str(uuid4())
    pool.add_conversation(conv_id=foreign, user_id='other', title='Private')
    await chat._save_message(pool, foreign, 'user', 'Other owner private message')
    offsets = []

    def post(url, *, json):
        async def enter():
            offsets.append(json['envelope']['offset'])
            status, _ = await ingest.receive(json, chat.config.BOBCLAW_SECRET)
            assert status == 200
            return SimpleNamespace(status=status)
        return Context(enter=enter)
    monkeypatch.setattr(chat.aiohttp, 'ClientSession', lambda **kw: Context(SimpleNamespace(post=post)))
    await chat._index_stored_conversation(pool, cid, 'owner')
    await chat._index_stored_conversation(pool, cid, 'owner')
    await chat._index_stored_conversation(pool, foreign, 'owner')
    assert offsets == [0, 100, 200] * 2
    rows = store.list_messages(cid, 'owner')
    assert len(rows) == 205 and rows[0]['content'] == 'Original message 0'
    assert rows[-1]['content'] == 'Original message 204' and rows[-1]['seq'] == 204
    assert store.list_messages(foreign, 'owner') == []
    assert store.list_messages(foreign, 'other') == []


async def test_index_failure_is_logged_after_an_unchanged_reply(indexed, monkeypatch, caplog):
    pool, cid = indexed
    async def relay(ws, *args):
        await ws.send_json({'type': 'message_complete', 'message_id': 'stored'})
    monkeypatch.setattr(chat, '_relay_chat_to_client', relay)
    pool.fetch = AsyncMock(side_effect=RuntimeError('index storage unavailable'))
    ws = SimpleNamespace(send_json=AsyncMock())
    await chat._stream_chat_to_client(ws, cid, {'content': 'Hi'}, {}, pool, 'owner')
    ws.send_json.assert_awaited_once_with({'type': 'message_complete', 'message_id': 'stored'})
    await asyncio.gather(*tuple(chat._INDEX_TASKS))
    assert 'Stored conversation indexing failed' in caplog.text
    assert ws.send_json.await_count == 1


async def test_large_stored_message_does_not_exceed_internal_request_ceiling(indexed, monkeypatch):
    pool, cid = indexed
    content = '\u2603' * 1_000_000
    await chat._save_message(pool, cid, 'assistant', content)
    def post(url, *, json):
        async def enter():
            assert len(chat.json.dumps(json).encode('utf-8')) < 1024 * 1024
            assert json['envelope']['rows'][0]['content'] is None
            status, _ = await ingest.receive(json, chat.config.BOBCLAW_SECRET)
            assert status == 200
            return SimpleNamespace(status=status)
        return Context(enter=enter)
    monkeypatch.setattr(chat.aiohttp, 'ClientSession', lambda **kw: Context(SimpleNamespace(post=post)))
    await chat._index_stored_conversation(pool, cid, 'owner')
    assert store.list_messages(cid, 'owner')[0]['content'] == content
