"""Keyword recall is available offline without swallowing security failures."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.memory.exceptions import ACLViolation, EmbedderUnavailable
from core.tools import memory_tools


def fact(fid, text, rank='normal'):
    return SimpleNamespace(fact_id=fid, body={'text': text}, confidence=SimpleNamespace(rank=rank),
                           generation_method='extract_facts_from_event')


@pytest.fixture
def memory(monkeypatch):
    mem = SimpleNamespace(
        retriever=SimpleNamespace(search=AsyncMock(side_effect=EmbedderUnavailable('local', 'down'))),
        fact_store=SimpleNamespace(query=AsyncMock(return_value=[])),
    )
    monkeypatch.setattr(memory_tools, '_singletons', lambda: mem)
    return mem


async def test_keyword_fallback_ranks_caps_and_omits_deprecated(memory):
    memory.fact_store.query.return_value = [
        fact('deprecated', 'repair builder', 'deprecated'), fact('unrelated', 'weather'),
        *[fact(str(n), 'Builder repair notes') for n in range(8)], fact('partial', 'builder'),
    ]
    result = json.loads(await memory_tools.memory_recall.ainvoke({'query': 'BUILDER repair'}))
    assert result['degraded'] == 'embedder_unavailable'
    assert result['coverage'] == 'stored_facts_only'
    assert [h['fact_id'] for h in result['hits']] == ['0', '1', '2', '3', '4']
    memory.fact_store.query.assert_awaited_once_with({})


async def test_degraded_no_match_is_explicit(memory):
    result = json.loads(await memory_tools.memory_recall.ainvoke({'query': 'unknown'}))
    assert result['degraded'] == 'embedder_unavailable'
    assert result['hits'] == []


async def test_keyword_store_failure_preserves_degraded_flag(memory):
    memory.fact_store.query.side_effect = OSError('offline')
    result = json.loads(await memory_tools.memory_recall.ainvoke({'query': 'builder'}))
    assert result['degraded'] == 'embedder_unavailable'
    assert result['error'] == 'keyword_search_unavailable'


async def test_security_failure_does_not_trigger_keyword_bypass(memory):
    memory.retriever.search.side_effect = ACLViolation('store', 'denied')
    result = await memory_tools.memory_recall.ainvoke({'query': 'builder'})
    assert result.startswith('[error:recall] ACLViolation')
    memory.fact_store.query.assert_not_awaited()


async def test_healthy_semantic_path_labels_where_a_hit_came_from(memory):
    memory.retriever.search.side_effect = None
    memory.retriever.search.return_value = [SimpleNamespace(content='semantic hit', source_fact_id=None)]
    assert await memory_tools.memory_recall.ainvoke({'query': 'builder'}) == 'Memory hits:\n- semantic hit\n  [conversation history]'
    memory.fact_store.query.assert_not_awaited()
