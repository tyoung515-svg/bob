"""The stored-row ingress fails closed; search explains local coverage."""
import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from core.context import backfill, ingest, store
from core.execution.api import lane_id
from core.execution import chat_records
from core.tools import conv_tools as ct
from test_gl7_assistant import history


def envelope(cid):
    now = int(time.time())
    return dict(lane=lane_id(), user_id='owner', conversation_id=cid, offset=0,
                previous_id='', iat=now, exp=now + 30,
                rows=[dict(id=str(uuid4()), role='user', content='daily planner',
                           created_at='2026-11-17T10:00:00+00:00')])


@pytest.mark.parametrize('change', ['signature', 'owner', 'lane', 'expired', 'outside_index',
                                   'unconfigured', 'oversized_page', 'missing_pool'])
async def test_ingress_rejects_invalid_authority_before_writing(history, monkeypatch, change):
    from core import lane_config
    starter = Mock()
    monkeypatch.setattr(backfill, 'ensure_started', starter)
    value = envelope(str(uuid4()))
    pool = SimpleNamespace(fetchval=AsyncMock(return_value=value['conversation_id']))
    monkeypatch.setattr(chat_records, 'POOL', pool)
    if change == 'owner':
        pool.fetchval.return_value = None
    elif change == 'lane':
        value['lane'] = 'other-lane'
    elif change == 'expired':
        value['iat'] -= 100
        value['exp'] -= 100
    elif change == 'outside_index':
        monkeypatch.setenv('CONTEXT_DB', str(history.lane.root.parent / 'outside.db'))
    elif change == 'unconfigured':
        monkeypatch.setattr(lane_config, 'configured_mode', lambda: False)
    elif change == 'oversized_page':
        value['rows'] *= 101
    elif change == 'missing_pool':
        monkeypatch.setattr(chat_records, 'POOL', None)
    proof = ingest.signature(value, 'test-secret') if change != 'signature' else 'invalid'
    write = AsyncMock()
    monkeypatch.setattr(store, 'record_stored_history', write)
    status, _ = await ingest.receive({'envelope': value, 'vouch': proof}, 'test-secret')
    assert status in {403, 404, 503}
    write.assert_not_called()
    starter.assert_not_called()
    if change not in {'owner', 'missing_pool'}:
        pool.fetchval.assert_not_awaited()


async def test_stored_empty_row_and_idempotent_replay(history, monkeypatch):
    value = envelope(str(uuid4()))
    value['rows'][0]['content'] = ''
    monkeypatch.setattr(chat_records, 'POOL', SimpleNamespace(fetchval=AsyncMock(return_value=value['conversation_id'])))
    body = {'envelope': value, 'vouch': ingest.signature(value, 'test-secret')}
    for _ in range(2):
        assert (await ingest.receive(body, 'test-secret'))[0] == 200
    rows = store.list_messages(value['conversation_id'], 'owner')
    assert len(rows) == 1 and rows[0]['content'] == '' and rows[0]['message_id'] == value['rows'][0]['id']
    assert store.list_messages(value['conversation_id'], 'other') == []


async def test_empty_result_explains_owner_scoped_coverage_and_keeps_all_words(history):
    cid, _ = history.add('HTML daily planner')
    history.add('A separate message', cid=cid)
    history.add('Private planner', owner='other')
    history.add('Current planner', cid=history.conversation)
    out = json.loads(await ct.conversations_find.ainvoke({'query': 'HTML separate'}))
    assert out['hits'] == []
    assert out['message'] == ('The index covers 1 of your earlier conversations in this lane. '
                              'A shorter query with one or two distinctive words may find more.')
    assert len(json.loads(await ct.conversations_find.ainvoke({'query': 'planner'}))['hits']) == 1


async def test_empty_new_index_is_reported_without_creating_it(history):
    from pathlib import Path
    out = json.loads(await ct.conversations_find.ainvoke({'query': 'planner'}))
    assert out['hits'] == [] and 'covers 0 of your earlier conversations' in out['message']
    assert not Path(store.db_path()).exists()


async def test_internal_route_receives_pages_and_reports_storage_failure(history, monkeypatch):
    from api import server
    value = envelope(str(uuid4()))
    monkeypatch.setattr(chat_records, 'POOL', SimpleNamespace(fetchval=AsyncMock(return_value=value['conversation_id'])))
    body = {'envelope': value, 'vouch': ingest.signature(value, server.config.BOBCLAW_SECRET)}
    request = SimpleNamespace(json=AsyncMock(return_value=body))
    assert (await server.index_stored_conversation(request)).status == 200
    monkeypatch.setattr(store, 'record_stored_history', lambda *args: (_ for _ in ()).throw(OSError('fixture')))
    assert (await server.index_stored_conversation(request)).status == 503
    request.json.side_effect = ValueError('invalid json')
    assert (await server.index_stored_conversation(request)).status == 400


@pytest.fixture
def older(history, monkeypatch):
    path = history.lane.root / 'memory.db'
    monkeypatch.setenv('MEMORY_SQLITE_PATH', str(path))
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE memory_events(insertion_order INTEGER PRIMARY KEY,kind TEXT,body_json TEXT)')
    canonical = {}
    original_fetch = history.pool.fetch

    def event(body, kind='agent_turn'):
        with sqlite3.connect(path) as db:
            db.execute('INSERT INTO memory_events(kind,body_json) VALUES(?,?)', (kind, json.dumps(body)))

    def add(count=2, owner='owner', proven=True, event_owner=None):
        cid = str(uuid4())
        history.catalog[cid] = dict(id=cid, user_id=owner, title='Older daily planner',
                                    face_id='assistant', is_archived=False)
        canonical[cid] = [dict(id=uuid4(), role='user' if i % 2 == 0 else 'assistant',
                              content=f'HTML daily planner {i}',
                              created_at=datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(seconds=i))
                          for i in range(count)]
        if proven:
            event(dict(conversation_id=cid, user_id=event_owner or owner))
        return cid

    async def fetch(sql, *args, **kwargs):
        if 'FROM messages m JOIN conversations c' not in sql:
            return await original_fetch(sql, *args, **kwargs)
        identifier, user, after_time, after_id, limit = args
        assert user == 'owner' and kwargs == {'timeout': 5} and limit == 100
        assert 'c.user_id=$2 AND c.is_archived=FALSE' in sql
        cid = str(identifier)
        metadata = history.catalog.get(cid)
        if not metadata or metadata['user_id'] != user or metadata['is_archived']:
            return []
        return [row for row in canonical[cid] if after_time is None or
                (row['created_at'], row['id']) > (after_time, after_id)][:limit]

    monkeypatch.setattr(history.pool, 'fetch', AsyncMock(side_effect=fetch))
    return SimpleNamespace(history=history, path=path, add=add, event=event, rows=canonical)


async def test_backfill_is_bounded_resumes_durable_cursors_and_rechecks_owner(older):
    cid = older.add(205)
    foreign = older.add(owner='other', event_owner='owner')  # Canonical recheck rejects this stale provenance.
    other = older.add(owner='other')
    unproven = older.add(proven=False)
    for _ in range(103):
        older.event(dict(user_id='other', conversation_id=str(uuid4())))
    older.event(dict(user_id='owner', conversation_id='invalid'))
    older.event(dict(user_id='owner', conversation_id=str(uuid4())), kind='untrusted_import')

    await backfill.step('owner')
    with store._connect() as db:
        assert db.execute('SELECT last_order FROM conv_backfill_sources WHERE user_id=?', ('owner',)).fetchone()[0] == 100
        jobs = [dict(r) for r in db.execute('SELECT * FROM conv_backfill_jobs')]
    assert {j['conversation_id'] for j in jobs} == {cid, foreign}
    assert all(j['offset'] <= 100 for j in jobs)
    # Continue using only persisted source/page positions, as after a restart.
    for _ in range(10):
        if await backfill.step('owner'):
            break
    else:
        pytest.fail('bounded backfill did not finish the supplied fixture')
    rows = store.list_messages(cid, 'owner')
    assert len(rows) == 205 and [r['seq'] for r in rows] == list(range(205))
    assert [r['message_id'] for r in rows] == [str(r['id']) for r in older.rows[cid]]
    for hidden in (foreign, other, unproven):
        assert store.list_messages(hidden, 'owner') == []
    assert store.list_messages(other, 'other') == []
    assert await backfill.step('owner') is True
    assert len(store.list_messages(cid, 'owner')) == 205


async def test_first_find_waits_for_backfill_and_a_capped_pass_resumes(older, monkeypatch):
    cid = older.add(205)
    monkeypatch.setattr(backfill, 'MAX_STEPS_PER_PASS', 1)
    first = json.loads(await ct.conversations_find.ainvoke({'query': 'HTML planner'}))
    assert first['hits'][0]['conversation_id'] == cid
    assert first['import_notice'] == ct.IMPORT_PENDING_NOTICE
    assert not backfill._RUNNING
    assert len(store.list_messages(cid, 'owner')) == 100
    # A capped pass is not marked done; subsequent discovery automatically resumes.
    monkeypatch.setattr(backfill, 'MAX_STEPS_PER_PASS', 60)
    await ct.conversations_find.ainvoke({'query': 'HTML planner'})
    await asyncio.gather(*list(backfill._RUNNING.values()))
    assert len(store.list_messages(cid, 'owner')) == 205
    result = json.loads(await ct.conversations_find.ainvoke({'query': 'HTML planner'}))
    assert result['hits'][0]['conversation_id'] == cid
    assert not backfill._RUNNING


@pytest.mark.parametrize('entry', ['turn', 'index'])
@pytest.mark.parametrize('broken_start', [False, True])
async def test_import_starts_early_without_blocking_turn_or_index(older, monkeypatch, entry, broken_start):
    from api import server
    from core.execution import workflow_context
    cid = older.add()
    original = backfill.ensure_started
    starter = Mock(side_effect=OSError('fixture start failed') if broken_start else original)
    monkeypatch.setattr(backfill, 'ensure_started', starter)
    # A running import cannot block either request, even if it never finishes.
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_step(user):
        entered.set()
        await release.wait()
        return True
    monkeypatch.setattr(backfill, 'step', blocked_step)
    try:
        if entry == 'index':
            value = envelope(cid)
            monkeypatch.setattr(older.history.pool, 'fetchval', AsyncMock(return_value=cid), raising=False)
            body = dict(envelope=value, vouch=ingest.signature(value, 'test-secret'))
            assert (await asyncio.wait_for(ingest.receive(body, 'test-secret'), 1))[0] == 200
            assert store.list_messages(cid, 'owner')[0]['content'] == 'daily planner'
        else:
            monkeypatch.setattr(workflow_context, 'conversation_run', AsyncMock(return_value=None))
            relay = AsyncMock(return_value='stream-response')
            monkeypatch.setattr(server, '_stream_graph_turn', relay)
            request = SimpleNamespace(
                app={server.GRAPH_KEY: object(), server.FACES_KEY: SimpleNamespace(get_allowed_tools=lambda _: [])},
                json=AsyncMock(return_value=dict(conversation_id=cid, content='hello', user_id='owner')))
            assert await asyncio.wait_for(server.chat(request), 1) == 'stream-response'
            assert relay.call_args.kwargs['graph_input']['user_id'] == 'owner'
        starter.assert_called_once_with('owner')
        if not broken_start:
            assert len(backfill._RUNNING) == 1
            await asyncio.wait_for(entered.wait(), 1)
    finally:
        release.set()
        await asyncio.gather(*list(backfill._RUNNING.values()))


async def test_search_during_running_import_waits_and_returns_imported_hit(older, monkeypatch):
    cid = older.add()
    entered, release = asyncio.Event(), asyncio.Event()
    original = backfill.step
    async def gated_step(user):
        entered.set()
        await release.wait()
        return await original(user)
    monkeypatch.setattr(backfill, 'step', gated_step)
    task = backfill.ensure_started('owner')
    await entered.wait()
    search = asyncio.create_task(ct.conversations_find.ainvoke({'query': 'HTML planner'}))
    try:
        await asyncio.sleep(0.02)
        assert not search.done()
        assert backfill.ensure_started('owner') is task
    finally:
        release.set()
        await task
    result = json.loads(await search)
    assert result['hits'][0]['conversation_id'] == cid
    assert 'import_notice' not in result


@pytest.mark.parametrize('with_hit', [False, True])
async def test_search_wait_timeout_explains_incomplete_import_without_cancelling(older, monkeypatch, with_hit):
    older.add()
    if with_hit:
        older.history.add('HTML planner already indexed')
    release = asyncio.Event()
    original = backfill.step
    async def gated_step(user):
        await release.wait()
        return await original(user)
    monkeypatch.setattr(backfill, 'step', gated_step)
    assert backfill.IMPORT_WAIT_SECONDS == 3.0
    monkeypatch.setattr(backfill, 'IMPORT_WAIT_SECONDS', 0.02)
    task = backfill.ensure_started('owner')
    try:
        result = json.loads(await asyncio.wait_for(ct.conversations_find.ainvoke({'query': 'HTML planner'}), 1))
        assert bool(result['hits']) == with_hit
        assert result['import_notice'] == ct.IMPORT_PENDING_NOTICE
        assert not task.done() and not task.cancelled()
    finally:
        release.set()
        await task


async def test_finished_import_adds_no_wait(older, monkeypatch):
    cid = older.add()
    await backfill.ensure_started('owner')
    waiter = AsyncMock(side_effect=AssertionError('finished import must not wait'))
    monkeypatch.setattr(backfill.asyncio, 'wait_for', waiter)
    result = json.loads(await ct.conversations_find.ainvoke({'query': 'HTML planner'}))
    assert result['hits'][0]['conversation_id'] == cid
    assert 'import_notice' not in result
    waiter.assert_not_called()


async def test_import_start_failure_does_not_fail_search(history, monkeypatch):
    cid, _ = history.add('HTML planner')
    monkeypatch.setattr(backfill, 'ensure_started', Mock(side_effect=OSError('fixture start failed')))
    result = json.loads(await ct.conversations_find.ainvoke({'query': 'HTML planner'}))
    assert result['hits'][0]['conversation_id'] == cid
    assert result['import_notice'] == ct.IMPORT_PENDING_NOTICE


@pytest.mark.parametrize('timeout', [False, True])
async def test_read_waits_for_missing_imported_message_or_explains_timeout(older, monkeypatch, timeout):
    cid = older.add()
    store.record_stored_history(cid, 'owner', [dict(id=str(older.rows[cid][0]['id']), role='user',
                                content='HTML planner', created_at=older.rows[cid][0]['created_at'].isoformat())], 0)
    token = ct.begin_history_turn()
    ct.history_turn.get()['found'].add(cid)
    release = asyncio.Event()
    original = backfill.step
    async def gated_step(user):
        await release.wait()
        return await original(user)
    monkeypatch.setattr(backfill, 'step', gated_step)
    monkeypatch.setattr(backfill, 'IMPORT_WAIT_SECONDS', 0.02 if timeout else 3.0)
    task = backfill.ensure_started('owner')
    reader = asyncio.create_task(ct.conversations_read.ainvoke(
        dict(conversation_id=cid, message_id=str(older.rows[cid][1]['id']))))
    try:
        if timeout:
            out = await asyncio.wait_for(reader, 1)
            assert out == ct.IMPORT_PENDING_NOTICE
            assert not task.done()
            partial = await ct.conversations_read.ainvoke(dict(conversation_id=cid))
            assert ct.IMPORT_PENDING_NOTICE in partial
            assert 'End of indexed conversation.' not in partial
        else:
            await asyncio.sleep(0.02)
            assert not reader.done()
            release.set()
            out = await reader
            assert str(older.rows[cid][1]['id']) in out
            assert ct.IMPORT_PENDING_NOTICE not in out
    finally:
        release.set()
        await task
        await reader
        ct.history_turn.reset(token)


async def test_backfill_never_opens_a_memory_source_outside_the_lane(older, monkeypatch):
    from core import lane_config
    from core.execution import chat_records
    outside = older.path.parent.parent
    monkeypatch.setattr(lane_config, 'get_lane', lambda: lane_config.LaneConfiguration(outside / 'other-lane', {}, {}, None))
    assert await backfill.step('owner') is True
    chat_records.POOL.fetch.assert_not_awaited()


async def test_backfill_failure_is_logged_and_does_not_escape_search(older, monkeypatch, caplog):
    older.add()
    monkeypatch.setattr(backfill, 'step', AsyncMock(side_effect=OSError('fixture unavailable')))
    result = await ct.conversations_find.ainvoke({'query': 'HTML planner'})
    assert json.loads(result)['hits'] == []
    await asyncio.gather(*list(backfill._RUNNING.values()))
    assert 'Earlier conversation backfill failed' in caplog.text
