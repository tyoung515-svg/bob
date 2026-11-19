"""BU2 saved-note corrections use real SQLite evidence and real T0 projection."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.memory._db import init_schema
from core.memory.event_log import SQLiteEventLog
from core.memory.fact_store import SQLiteFactStore
from core.memory.models import Hit, RankedResults
from core.memory.exceptions import EmbedderUnavailable
from core.memory.retriever import MemoryRetriever
from core.memory.writer.ledger import CompletionLedger
from core.memory.writer.tasks import ProjectVerbatimTask
from core.tools import memory_tools as mt
from memory.test_w3_writer import FakeEmbedder, FakeProvider


@pytest.fixture(autouse=True)
def mock_redis():
    yield  # These SQLite/retriever tests never invoke execute or Redis.


@pytest.fixture
async def notes(tmp_path, monkeypatch):
    db = tmp_path / 'memory.db'
    await init_schema(db)
    ledger = CompletionLedger(db)
    await ledger.initialize()
    facts = SQLiteFactStore(db)
    provider = FakeProvider()

    def query(store, vector, k, filters, offset=0):
        hits = [Hit(id=p.id, score=0.9, payload=p.payload) for p in provider.points.values()]
        return RankedResults(hits=hits[offset:offset+k], provider_id='fake', latency_ms=0)

    provider.query_vector = query
    embedder = FakeEmbedder()
    retriever = MemoryRetriever(embedder, provider, facts, 'test',
        SimpleNamespace(get=lambda key: None, keyword_fallback=True),
        SimpleNamespace(append=lambda row: None), t0_recall_enabled=True)
    mem = SimpleNamespace(event_log=SQLiteEventLog(db), fact_store=facts, retriever=retriever,
                         writer=ProjectVerbatimTask(ledger, embedder, provider, 'test'))
    monkeypatch.setattr(mt, '_singletons', lambda: mem)
    tokens = [mt.memory_tool_user_id.set('owner'), mt.memory_tool_face_id.set('assistant'),
              mt.memory_tool_conversation_id.set('conversation')]
    receipt = await mt.memory_save.ainvoke({'text': 'My favourite tea is green.'})
    assert 'index=completed' in receipt
    mem.original = (await facts.query({}))[0]
    mem.provider, mem.embedder = provider, embedder
    yield mem
    for var, token in zip([mt.memory_tool_user_id, mt.memory_tool_face_id, mt.memory_tool_conversation_id], tokens):
        var.reset(token)


async def test_recall_ids_and_retirement_blocks_both_stores(notes):
    old = notes.original
    result = await mt.memory_recall.ainvoke({'query': 'green tea'})
    assert f'saved note fact_id={old.fact_id}' in result
    result = await mt.memory_retire.ainvoke({'fact_id': old.fact_id, 'reason': 'This preference is out of date.'})
    assert 'Retired saved note' in result
    retired = await notes.fact_store.get(old.fact_id)
    assert retired.confidence.rank == 'deprecated'
    audit = retired.body['retirement']
    event = await notes.event_log.get(audit['event_id'])
    assert event.hash and event.prev_hash
    assert audit['user_id'] == 'owner' and audit['face_id'] == 'assistant'
    assert audit['conversation_id'] == 'conversation' and audit['ts'] == event.ts
    assert event.body['reason'] == 'This preference is out of date.'
    # The stale vector still exists, but neither semantic nor lexical recall
    # (which reconstructs the original event chunks) may serve it.
    assert notes.provider.points
    assert await mt.memory_recall.ainvoke({'query': 'green tea'}) == 'No memory found for that query.'
    notes.embedder.embed = AsyncMock(side_effect=EmbedderUnavailable('local-test', 'offline'))
    assert await mt.memory_recall.ainvoke({'query': 'green tea'}) == 'No memory found for that query.'
    again = await mt.memory_retire.ainvoke({'fact_id': old.fact_id, 'reason': 'Already done.'})
    assert 'already retired' in again and not again.startswith('[error:')


async def test_correct_saves_searchable_replacement_then_retires(notes):
    old = notes.original
    result = await mt.memory_correct.ainvoke({'fact_id': old.fact_id,
        'text': 'My favourite tea is black.', 'reason': 'The owner corrected the preference.'})
    assert 'Corrected saved note' in result
    retired = await notes.fact_store.get(old.fact_id)
    new_id = retired.body['retirement']['replacement_fact_id']
    assert new_id in retired.body['retirement']['reason']
    new = await notes.fact_store.get(new_id)
    assert new.body['text'] == 'My favourite tea is black.'
    assert new.confidence.rank == 'normal'
    hits = await notes.retriever.search('tea')
    assert {h.source_fact_id for h in hits} == {new_id}
    # Even re-saving exactly the retired text creates a fresh searchable note.
    receipt = await mt.memory_save.ainvoke({'text': old.body['text']})
    assert 'index=completed' in receipt
    assert any(old.body['text'] in h.content for h in await notes.retriever.search('tea'))


@pytest.mark.parametrize('failure', ['append', 'index', 'fact', 'disabled'])
async def test_correct_preserves_old_note_on_new_save_failure(notes, monkeypatch, failure):
    if failure == 'append':
        monkeypatch.setattr(notes.event_log, 'atomic_append', AsyncMock(side_effect=OSError('disk full')))
    elif failure == 'index':
        monkeypatch.setattr(notes.writer, 'project', AsyncMock(side_effect=OSError('offline')))
    elif failure == 'fact':
        monkeypatch.setattr(notes.fact_store, 'put', AsyncMock(side_effect=OSError('disk full')))
    else:
        notes.writer = None
    result = await mt.memory_correct.ainvoke({'fact_id': notes.original.fact_id,
        'text': 'Replacement note.', 'reason': 'The owner corrected it.'})
    assert result.startswith('[error:correction_save]') and 'left active' in result
    assert (await notes.fact_store.get(notes.original.fact_id)).confidence.rank == 'normal'


@pytest.mark.parametrize('tool', [mt.memory_retire, mt.memory_correct])
@pytest.mark.parametrize('kind', ['extracted', 'foreign', 'unknown', 'starter', 'imported'])
async def test_mutations_refuse_nonowned_or_nonsaved_facts(notes, tool, kind):
    old = notes.original
    if kind == 'unknown':
        fid = 'missing'
    else:
        fact = replace(old, fact_id='refused', generation_method={
            'extracted': 'extract_facts_from_event', 'starter': 'manual_seed',
            'imported': 'family_import', 'foreign': 'face_memory_save:v1'}[kind])
        if kind == 'foreign':
            event = await notes.event_log.atomic_append({'source': 'memory_save_tool', 'user_id': 'other'})
            fact = replace(fact, source_event_id=event.event_id)
        await notes.fact_store.put(fact)
        fid = fact.fact_id
    args = dict(fact_id=fid, reason='The owner corrected it.')
    if tool is mt.memory_correct:
        args['text'] = 'Replacement.'
    assert (await tool.ainvoke(args)).startswith('[error:')
    assert (await notes.fact_store.get(old.fact_id)).confidence.rank == 'normal'


async def test_stored_fact_fallback_labels_ids_and_hides_retired(notes, monkeypatch):
    notes.retriever.search = AsyncMock(side_effect=EmbedderUnavailable('local-test', 'offline'))
    result = json.loads(await mt.memory_recall.ainvoke({'query': 'tea'}))
    assert result['hits'][0]['fact_id'] == notes.original.fact_id
    assert result['hits'][0]['kind'] == 'saved note'
    await mt.memory_retire.ainvoke({'fact_id': notes.original.fact_id, 'reason': 'Out of date.'})
    assert json.loads(await mt.memory_recall.ainvoke({'query': 'tea'}))['hits'] == []


async def test_partial_correction_keeps_the_new_save_receipt(notes, monkeypatch):
    monkeypatch.setattr(mt, '_retire_note', AsyncMock(side_effect=OSError('disk full')))
    result = await mt.memory_correct.ainvoke({'fact_id': notes.original.fact_id,
        'text': 'The replacement preference.', 'reason': 'The owner corrected it.'})
    assert result.startswith('[error:correction_retire]')
    replacement = next(f for f in await notes.fact_store.query({}) if f.fact_id != notes.original.fact_id)
    assert replacement.fact_id in result and 'index=completed' in result
    assert (await notes.fact_store.get(notes.original.fact_id)).confidence.rank == 'normal'


async def test_retired_filter_uses_existing_memory_route(notes, monkeypatch):
    from api import server
    monkeypatch.setattr(server, 'get_memory', lambda: notes)
    monkeypatch.setattr(server.config, 'MEMORY_ENABLED', True)
    await mt.memory_retire.ainvoke({'fact_id': notes.original.fact_id, 'reason': 'Out of date.'})
    response = await server.list_memory_facts(SimpleNamespace(query={'status': 'retired'}))
    rows = json.loads(response.text)
    assert len(rows) == 1 and rows[0]['fact_id'] == notes.original.fact_id
    assert rows[0]['retirement']['reason'] == 'Out of date.'
    assert rows[0]['confidence']['rank'] == 'deprecated'


@pytest.mark.parametrize('face', ['assistant', 'planner'])
async def test_bu7_saved_note_uses_invoking_face_and_owner_provenance(notes, face):
    token = mt.memory_tool_face_id.set(face)
    try:
        receipt = await mt.memory_save.ainvoke({'text': 'The owner chose Godot for the agreed game module.'})
        assert 'index=completed' in receipt
        saved = next(f for f in await notes.fact_store.query({})
                     if f.body.get('text') == 'The owner chose Godot for the agreed game module.')
        event = await notes.event_log.get(saved.source_event_id)
        assert saved.body['face_id'] == face
        assert event.body['face_id'] == face and event.body['user_id'] == 'owner'
        assert event.body['conversation_id'] == 'conversation'
        assert 'Godot' in await mt.memory_recall.ainvoke({'query': 'Godot'})
    finally:
        mt.memory_tool_face_id.reset(token)
