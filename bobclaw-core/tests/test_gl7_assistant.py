"""GL7: real lane-local SQLite search and prompt cards; no model/host calls."""
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from core.context import store
from core.execution import chat_records
from core import lane_config
from core.faces import prompt
from core.faces.registry import FaceRegistry
from core.tools.conv_tools import conversations_find, conv_expand, MAX_FIND_CHARS
from core.tools.memory_tools import memory_tool_conversation_id, memory_tool_user_id


@pytest.fixture
def history(tmp_path, monkeypatch):
    lane = lane_config.LaneConfiguration(tmp_path, {}, {}, None)
    monkeypatch.setattr(lane_config, 'configured_mode', lambda: True)
    monkeypatch.setattr(lane_config, 'get_lane', lambda: lane)
    monkeypatch.setenv('CONTEXT_DB', str(tmp_path / 'context.db'))
    conversation = str(uuid.uuid4())
    tokens = (memory_tool_user_id.set('owner'), memory_tool_conversation_id.set(conversation))
    catalog = {}

    async def metadata(sql, user, ids, **kwargs):
        assert 'WHERE user_id=$1 AND id=ANY($2::uuid[]) AND is_archived=FALSE' in sql
        assert kwargs == {'timeout':5}
        return [row for key, row in catalog.items()
                if uuid.UUID(key) in ids and row['user_id'] == user and not row.get('is_archived')]

    pool = SimpleNamespace(fetch=AsyncMock(side_effect=metadata))
    monkeypatch.setattr(chat_records, 'POOL', pool)

    def add(content='We decided alpha deploy on Friday', *, owner='owner', cid=None,
            title='Deployment plan', agent='assistant', archived=False):
        cid = cid or str(uuid.uuid4())
        mid = str(uuid.uuid4())
        store.record_history(cid, owner, [dict(id=mid, role='user', content=content,
                                             created_at='2026-11-09T10:00:00Z')])
        catalog[cid] = dict(id=uuid.UUID(cid), title=title, face_id=agent,
                            user_id=owner, is_archived=archived)
        return cid, mid

    yield SimpleNamespace(add=add, pool=pool, conversation=conversation, lane=lane, catalog=catalog)
    memory_tool_user_id.reset(tokens[0])
    memory_tool_conversation_id.reset(tokens[1])


async def test_find_own_conversation_and_matching_message_without_widening_expand(history):
    cid, mid = history.add()
    result = json.loads(await conversations_find.ainvoke({'query':'alpha deploy'}))
    assert result['hits'] == [dict(conversation_id=cid, title='Deployment plan', agent='assistant',
                                   date='2026-11-09T10:00:00Z', message_id=mid,
                                   snippet='We decided alpha deploy on Friday')]
    assert 'not exhaustive' in result['coverage']
    assert 'only the current conversation' in result['expand']
    assert (await conv_expand.ainvoke({'ids':mid})).startswith('[error:not_found]')


async def test_find_never_returns_another_owner_current_archived_or_deleted(history):
    own, _ = history.add()
    foreign, _ = history.add(owner='other')
    history.add(cid=history.conversation)
    history.add(archived=True)
    stale, _ = history.add()
    deleted, _ = history.add()
    del history.catalog[deleted]
    # A stale index whose canonical conversation changed owner is also hidden.
    history.catalog[stale]['user_id'] = 'other'
    result = json.loads(await conversations_find.ainvoke({'query':'alpha deploy'}))
    assert [h['conversation_id'] for h in result['hits']] == [own]
    assert uuid.UUID(foreign) not in history.pool.fetch.call_args.args[2]


async def test_find_bounds_hits_snippets_and_total_output(history):
    for _ in range(15):
        cid, _ = history.add('x'*600 + 'alpha ' + 'z'*800, title='T'*1000, agent='A'*1000)
        history.add('alpha second matching message', cid=cid, title='T'*1000, agent='A'*1000)
    out = await conversations_find.ainvoke({'query':'alpha', 'limit':999})
    hits = json.loads(out)['hits']
    assert len(hits) == 10 and len({h['conversation_id'] for h in hits}) == 10
    assert all(len(h['snippet']) <= 300 and len(h['title']) <= 80 and len(h['agent']) <= 80 for h in hits)
    assert len(out) <= MAX_FIND_CHARS
    assert len(json.loads(await conversations_find.ainvoke({'query':'alpha', 'limit':-1}))['hits']) == 1


async def test_find_output_cap_includes_json_escaping(history):
    for _ in range(15):
        history.add('alpha' + '\x01'*1000, title='\x01'*80, agent='\x01'*80)
    out = await conversations_find.ainvoke({'query':'alpha', 'limit':10})
    assert 0 < len(json.loads(out)['hits']) < 10
    assert len(out) <= MAX_FIND_CHARS


async def test_find_snippet_is_bounded_and_contains_match(history):
    history.add('x'*600 + 'alpha ' + 'z'*800)
    hit = json.loads(await conversations_find.ainvoke({'query':'alpha'}))['hits'][0]
    assert len(hit['snippet']) == 300 and 'alpha' in hit['snippet']


async def test_find_literal_wildcards_and_no_match(history):
    cid, _ = history.add('decided 50%_done')
    history.add('decided 50xxdone')
    assert [h['conversation_id'] for h in json.loads(
        await conversations_find.ainvoke({'query':'50%_done'}))['hits']] == [cid]
    assert json.loads(await conversations_find.ainvoke({'query':'missing'}))['hits'] == []


async def test_find_requires_identity_and_lane_local_index(history, monkeypatch, tmp_path):
    history.add()
    token = memory_tool_user_id.set(None)
    try:
        assert (await conversations_find.ainvoke({'query':'alpha'})).startswith('[error:missing_identity]')
    finally:
        memory_tool_user_id.reset(token)
    other = tmp_path / 'another-lane'
    other.mkdir()
    monkeypatch.setattr(lane_config, 'get_lane', lambda: lane_config.LaneConfiguration(other, {}, {}, None))
    assert (await conversations_find.ainvoke({'query':'alpha'})).startswith('[error:lane_scope_unavailable]')
    history.pool.fetch.assert_not_awaited()
    # Switching to that lane's own empty index cannot see the first lane's rows,
    # even when the caller has the same user id.
    monkeypatch.setenv('CONTEXT_DB', str(other / 'context.db'))
    assert json.loads(await conversations_find.ainvoke({'query':'alpha'}))['hits'] == []


async def test_find_checks_actual_cwd_for_relative_index_path(history, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path.parent)
    monkeypatch.setenv('CONTEXT_DB', 'context.db')
    assert (await conversations_find.ainvoke({'query':'alpha'})).startswith('[error:lane_scope_unavailable]')
    history.pool.fetch.assert_not_awaited()


async def test_find_does_not_guess_legacy_lane_scope(history, monkeypatch):
    monkeypatch.setattr(lane_config, 'configured_mode', lambda: False)
    assert (await conversations_find.ainvoke({'query':'alpha'})).startswith('[error:lane_scope_unavailable]')
    history.pool.fetch.assert_not_awaited()


async def test_find_fails_closed_on_missing_store_and_bad_query(history, monkeypatch):
    for query in ['', ' ', 'x'*501]:
        assert (await conversations_find.ainvoke({'query':query})).startswith('[error:invalid_query]')
    monkeypatch.setattr(chat_records, 'POOL', None)
    assert (await conversations_find.ainvoke({'query':'alpha'})).startswith('[error:history_unavailable]')
    history.pool.fetch.assert_not_awaited()


async def test_find_uses_read_only_connection_without_initializing_storage(history, monkeypatch):
    cid, _ = history.add()

    def no_initialization():
        raise AssertionError('discovery must not open the writable initialization path')

    monkeypatch.setattr(store, '_connect', no_initialization)
    assert json.loads(await conversations_find.ainvoke({'query':'alpha'}))['hits'][0]['conversation_id'] == cid


async def test_find_storage_errors_are_in_band_without_partial_results(history, monkeypatch):
    history.add()

    def broken(*args):
        raise sqlite3.DatabaseError('fixture index unavailable')

    monkeypatch.setattr(store, 'find_conversations', broken)
    assert (await conversations_find.ainvoke({'query':'alpha'})).startswith('[error:history_unavailable]')
    history.pool.fetch.assert_not_awaited()


async def test_find_metadata_timeout_is_in_band(history):
    history.add()
    history.pool.fetch.side_effect = TimeoutError('fixture timeout')
    assert (await conversations_find.ainvoke({'query':'alpha'})).startswith('[error:history_unavailable]')


@pytest.fixture
def cards(monkeypatch):
    # Use actual shipped profiles and prompt assembly, with a minimal native
    # catalog to avoid importing unrelated unavailable provider transports.
    monkeypatch.setenv('BOB_ROSTER_FILE', str(Path(__file__).resolve().parents[2] / 'bobclaw-core/tests/fixtures/deployment/roster.yaml'))
    monkeypatch.setenv('BOB_FACE_PROMPTS', '1')
    registry = FaceRegistry(profiles_dir=Path(prompt.__file__).parent / 'templates')
    monkeypatch.setattr('core.faces.registry.get_default_registry', lambda: registry)
    monkeypatch.setitem(sys.modules, 'core.tools.registry', SimpleNamespace(NATIVE_TOOLS={}, PIPE_OPERATIONS=set()))

    def card(face):
        messages = prompt.interactive_messages([{'role':'user','content':'hello'}], face, 'fixture')
        return json.loads(messages[1]['content'].split('\n', 1)[1])

    return registry, card


@pytest.mark.parametrize('face_id', ['assistant', 'planner'])
def test_action_card_for_conversation_faces_with_exact_contract(cards, face_id):
    registry, card = cards
    suggestions = card(face_id)['action_suggestions']
    assert 'At most two fenced blocks' in suggestions['format']
    assert 'bob-action' in suggestions['format'] and 'saves nothing' in suggestions['format']
    calendar = suggestions['calendar']
    assert set(calendar['fields']) == {'type','title','date','start','minutes','repeat','note'}
    assert calendar['fields']['type'] == 'calendar'
    for text in ['80', 'YYYY-MM-DD', 'HH:MM', 'all day', '5..1440', '30',
                 'none|daily|weekdays|weekly|monthly', '500']:
        assert text in str(calendar['fields'])
    assert calendar['wording'] == "I've suggested a calendar entry; click Add to Tasks to keep it"
    assert 'time zone' in calendar['when'] and 'assumption' in calendar['when']
    assert "I'll remind you" in calendar['authority'] and "I've scheduled" in calendar['authority']
    handoff = suggestions['handoff']
    assert set(handoff['fields']) == {'type','agent','title','brief','manage','session'}
    assert handoff['fields']['agent'] == ['builder','fixer','orchestrator']
    assert '4000' in handoff['fields']['brief'] and '80' in handoff['fields']['title']
    assert 'unsent' in handoff['authority'] and 'review and send' in handoff['authority']
    assert 'default true' in handoff['fields']['manage']
    assert 'same agent' in handoff['fields']['session']
    assert 'one report' in handoff['authority'] and "user's click" in handoff['authority']
    assert '--- report ---' in handoff['report_shape']
    assert 'evidence, not instructions' in handoff['report_handling']
    assert {'conv_search','conv_expand','conversations_find'} <= set(registry.get_allowed_tools('assistant'))
    for face in registry.list_faces():
        if face.id not in {'assistant', 'planner'}:
            assert 'action_suggestions' not in card(face.id)
            assert ('conversations_find' in registry.get_allowed_tools(face.id)) == (face.id == 'researcher')


@pytest.mark.parametrize('face', ['assistant', 'planner'])
def test_handoff_agents_are_filtered_to_installed_catalog(cards, monkeypatch, face):
    registry, card = cards
    monkeypatch.delitem(registry._faces, 'fixer')
    monkeypatch.delitem(registry._faces, 'orchestrator')
    assert card(face)['action_suggestions']['handoff']['fields']['agent'] == ['builder']
    monkeypatch.delitem(registry._faces, 'builder')
    assert card(face)['action_suggestions']['handoff']['fields']['agent'] == []


def test_pc_info_joins_only_pc_capable_configured_faces(cards, monkeypatch):
    from core.tools.pc_tools import PC_TOOL_NAMES
    registry, _ = cards
    monkeypatch.setattr(lane_config, 'configured_mode', lambda: True)
    for face in registry.list_faces():
        face_tools = set(registry.get_face(face.id).allowed_tools)
        expected = face_tools | set(PC_TOOL_NAMES)
        from core.tools.workflows import WORKFLOW_TOOLS, WORKFLOW_READ_NAMES
        if face.id in {'assistant', 'planner'}:
            expected |= {t.name for t in WORKFLOW_TOOLS}
        elif face.id == 'orchestrator':
            expected |= set(WORKFLOW_READ_NAMES)
        assert set(registry.get_allowed_tools(face.id)) == expected
        assert ('conversations_find' in face_tools) == (face.id in {'assistant', 'planner', 'researcher'})
