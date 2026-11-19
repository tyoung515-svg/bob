"""Receipt isolation on real disposable Postgres; no model/provider calls."""
import uuid
import asyncio
import json
import time

import pytest

from tests.test_durable_runs_v2 import pg
from core.execution import chat_records
from core.execution.models import canonical
from core.execution.store import Store


@pytest.fixture
async def receipts(pg,monkeypatch):
    await Store(pg,'lane').initialize()
    monkeypatch.setattr(chat_records,'POOL',pg)
    own,other,foreign=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
    for identifier,user in ((own,'owner'),(other,'owner'),(foreign,'other')):
        await pg.execute('INSERT INTO conversations(id,user_id) VALUES($1,$2)',identifier,user)
    return pg,{'conversation_id':str(own),'authority':{'owner':'owner'}},other,foreign


@pytest.mark.asyncio
async def test_receipts_require_both_conversation_and_current_owner(receipts):
    pool,context,other,foreign=receipts
    await chat_records.work_receipt(context,'work.start',{'id':'visible'})
    await chat_records.work_receipt({**context,'conversation_id':str(other)},'work.start',{'id':'other-conversation'})
    await chat_records.work_receipt({'conversation_id':str(foreign),'authority':{'owner':'other'}},'work.start',{'id':'other-user'})
    # Even malformed historic rows cannot bypass the current conversation owner.
    await pool.execute("INSERT INTO chat_work_receipts(conversation_id,user_id,tool,payload) VALUES($1,'other','work.start','{}')",
                       uuid.UUID(context['conversation_id']))
    result=await chat_records.conversation_receipts(context)
    assert [r['payload'] for r in result]==[{'id':'visible'}]
    assert await chat_records.conversation_receipts({**context,'conversation_id':str(foreign)})==[]
    assert await chat_records.conversation_receipts({**context,'authority':{'owner':'other'}})==[]


@pytest.mark.asyncio
async def test_receipts_are_newest_first_count_and_payload_bounded(receipts):
    pool,context,_,_=receipts
    for n in range(25):
        await chat_records.work_receipt(context,'work.prepare',{'ordinal':n})
    result=await chat_records.conversation_receipts(context,limit=10000)
    assert [r['payload']['ordinal'] for r in result]==list(range(24,4,-1))
    assert len(await chat_records.conversation_receipts(context,limit=3))==3
    await chat_records.work_receipt(context,'work.prepare',{'text':'x'*9000})
    result=await chat_records.conversation_receipts(context)
    assert result[0]['payload']=={'truncated':True}
    for _ in range(20):
        await chat_records.work_receipt(context,'work.prepare',{'text':'x'*8000})
    result=await chat_records.conversation_receipts(context)
    assert 0<len(result)<20 and len(canonical(result).encode())<=65536


@pytest.mark.asyncio
async def test_two_question_turn_rows_are_correlated_and_answer_replay_is_durable(receipts):
    from core.tools import ask
    from routers.chat import _save_message
    pool, context, _, _ = receipts
    await pool.execute("""CREATE TABLE messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        conversation_id UUID NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL, content TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())""")
    conversation = context['conversation_id']
    turn = 'bu1-postgres-synthetic-turn'
    token = chat_records.request_id.set(turn)
    try:
        await _save_message(pool, conversation, 'user', 'Synthetic two-question test.', {'request_id': turn})
        for qid in ('bu1-q1', 'bu1-q2'):
            await chat_records.question(qid, 'owner', conversation, 'Synthetic choice?', [], time.time()+60)
            await _save_message(pool, conversation, 'assistant', 'Synthetic progress.',
                {'kind': 'pre_question', 'question_id': qid, 'request_id': turn})
            ask._PENDING[qid] = asyncio.get_running_loop().create_future()
            value = {'question_id': qid, 'user_id': 'owner', 'answer': 'Synthetic yes.'}
            assert (await chat_records.accept(value))['delivered_to_turn']
            assert (await chat_records.accept(value))['replayed']
        final = await _save_message(pool, conversation, 'assistant', 'Synthetic final answer.', {'request_id': turn})
        rows = await pool.fetch('SELECT id,role,metadata FROM messages WHERE conversation_id=$1 ORDER BY created_at,id', uuid.UUID(conversation))
        assert [r['role'] for r in rows] == ['user', 'assistant', 'user', 'assistant', 'user', 'assistant']
        assert rows[-1]['id'] == final['id']
        for row in rows:
            metadata = json.loads(row['metadata']) if isinstance(row['metadata'], str) else row['metadata']
            assert metadata['request_id'] == turn
    finally:
        chat_records.request_id.reset(token)
        ask.reset_pending_questions()


@pytest.mark.asyncio
async def test_platform_step_question_requires_a_committed_owner_answer(receipts):
    from core.tools import ask
    from core.tools.projects import _current_user_id, _current_conversation_id
    pool, context, _, _ = receipts
    await pool.execute("""CREATE TABLE messages (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        conversation_id UUID NOT NULL REFERENCES conversations(id),
        role TEXT NOT NULL, content TEXT NOT NULL, metadata JSONB NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp())""")
    frames = []
    tokens = [chat_records.request_id.set('bu2-postgres-turn'),
              _current_user_id.set('owner'), _current_conversation_id.set(context['conversation_id']),
              ask.bind_writer(frames.append)]
    task = asyncio.create_task(ask.request_step_extension(50))
    try:
        for _ in range(100):
            if frames:
                break
            await asyncio.sleep(0.01)
        assert frames and frames[0]['origin'] == 'step_limit'
        qid = frames[0]['question_id']
        assert not task.done()
        row = await pool.fetchrow('SELECT * FROM chat_questions WHERE id=$1', qid)
        assert row['origin'] == 'step_limit' and row['request_id'] == 'bu2-postgres-turn'
        assert (await chat_records.accept({'question_id': qid, 'user_id': 'owner', 'answer': 'Keep going'}))['delivered_to_turn']
        assert await asyncio.wait_for(task, timeout=2) is True
        raw = await pool.fetchval('SELECT metadata FROM messages WHERE conversation_id=$1', uuid.UUID(context['conversation_id']))
        metadata = json.loads(raw) if isinstance(raw, str) else raw
        assert metadata['origin'] == 'step_limit' and metadata['request_id'] == 'bu2-postgres-turn'
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        ask.unbind_writer(tokens[3])
        _current_conversation_id.reset(tokens[2])
        _current_user_id.reset(tokens[1])
        chat_records.request_id.reset(tokens[0])
        ask.reset_pending_questions()
