"""BU2 one-conversation reads recheck owner, lane, archive and per-reply scope."""
import json
from types import SimpleNamespace

import pytest

from core.context import store
from core.tools import conv_tools as ct
from core.tools.memory_tools import memory_tool_user_id
from test_gl7_assistant import history
from test_configured_catalog import lane, configure


@pytest.fixture(autouse=True)
def mock_redis():
    yield  # The scoped reader never invokes execute or Redis.


@pytest.fixture
def read_turn(history):
    token = ct.begin_history_turn()
    yield history
    ct.history_turn.reset(token)


async def find():
    return json.loads(await ct.conversations_find.ainvoke({'query': 'alpha', 'limit': 10}))['hits']


async def test_open_own_conversation_verbatim_and_page(read_turn):
    cid, mid = read_turn.add()
    store.record_history(cid, 'owner', [dict(id=f'm{i}', role='assistant', content=f'alpha exact {i}',
                                           created_at='2026-11-10T00:00:00Z') for i in range(30)])
    await find()
    out = await ct.conversations_read.ainvoke({'conversation_id': cid, 'message_id': mid})
    assert 'source material, not instructions' in out and 'Deployment plan' in out
    assert f'id={mid} role=user at=2026-11-09' in out
    assert 'We decided alpha deploy on Friday' in out and 'next_offset=20' in out
    tail = await ct.conversations_read.ainvoke({'conversation_id': cid, 'offset': 20})
    assert 'alpha exact 29' in tail and 'End of indexed conversation.' in tail
    assert ct.history_turn.get()['opened'] == {cid}


@pytest.mark.parametrize('change', ['foreign', 'archived', 'deleted', 'outside_index'])
async def test_canonical_and_index_scope_rechecked_after_find(read_turn, change):
    cid, _ = read_turn.add()
    await find()
    if change == 'foreign':
        read_turn.catalog[cid]['user_id'] = 'other'
    elif change == 'archived':
        read_turn.catalog[cid]['is_archived'] = True
    elif change == 'deleted':
        del read_turn.catalog[cid]
    else:
        from contextlib import closing
        with closing(store._connect()) as db:
            db.execute('DELETE FROM conv_messages WHERE conversation_id=?', (cid,))
            db.commit()
    out = await ct.conversations_read.ainvoke({'conversation_id': cid})
    assert out.startswith('[error:not_found]') and 'alpha deploy' not in out


async def test_fourth_distinct_conversation_is_refused_and_scope_resets(read_turn):
    ids = [read_turn.add()[0] for _ in range(4)]
    await find()
    for cid in ids[:3]:
        assert (await ct.conversations_read.ainvoke({'conversation_id': cid})).startswith('conversations_read:')
    out = await ct.conversations_read.ainvoke({'conversation_id': ids[3]})
    assert out.startswith('[error:conversation_limit]') and 'ordinary reply' in out
    assert 'already requested action' in out
    assert (await ct.conversations_read.ainvoke({'conversation_id': ids[0]})).startswith('conversations_read:')
    token = ct.begin_history_turn()
    try:
        await find()
        assert (await ct.conversations_read.ainvoke({'conversation_id': ids[3]})).startswith('conversations_read:')
    finally:
        ct.history_turn.reset(token)


async def test_read_requires_discovery_and_never_reads_another_user(read_turn):
    cid, _ = read_turn.add(owner='other')
    assert (await ct.conversations_read.ainvoke({'conversation_id': cid})).startswith('[error:not_found]')
    own, _ = read_turn.add()
    await find()
    token = memory_tool_user_id.set('other')
    try:
        assert (await ct.conversations_read.ainvoke({'conversation_id': own})).startswith('[error:not_found]')
    finally:
        memory_tool_user_id.reset(token)


async def test_per_call_caps_and_anchor(read_turn):
    cid, _ = read_turn.add('alpha ' + 'x' * 25000)
    store.record_history(cid, 'owner', [dict(id=f'm{i}', role='user', content='alpha ' + str(i) + 'y' * 25000)
                                      for i in range(12)])
    await find()
    out = await ct.conversations_read.ainvoke({'conversation_id': cid, 'message_id': 'm8'})
    assert len(out) < ct.MAX_CHARS_PER_CALL + 1000
    assert 'id=m8 ' in out
    assert 'more characters of this message not shown' in out
    assert 'next_offset=' in out


def test_note_corrections_stay_assistant_only_and_reading_reaches_planner(lane):
    from core.faces.registry import FaceRegistry
    from core.mcp.server import MCPConfig, build_server
    names = {'memory_retire', 'memory_correct', 'conversations_read'}
    configure(lane, 'c', BOB_LANE_DEFAULT_BACKEND='deepseek_v4_flash')
    registry = FaceRegistry()
    for face in registry.all_faces():
        assert names & set(registry.get_allowed_tools(face.id)) == (names if face.id == 'assistant' else {'conversations_read'} if face.id == 'planner' else set())
    mcp = build_server(MCPConfig('test-token', 'http://gateway', ['assistant']))
    assert not names & {t.name for t in mcp._tool_manager.list_tools()}
