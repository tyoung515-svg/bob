from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory import FactStore, MemoryRetriever
from core.memory.exceptions import ACLViolation, RetrievalProviderError
from core.memory.models import ConfidenceStub, Fact, RetrievedChunk
from core.nodes.recall import recall_node


def _state_with_last_user(text: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": text},
        ]
    }


def _make_fact(fact_id: str = "fact-1", body: dict | None = None) -> Fact:
    return Fact(
        fact_id=fact_id,
        generation_method="manual",
        body=body or {"text": "test content"},
        source_event_id="evt-1",
        input_hash="abc123",
        confidence=ConfidenceStub(),
        ts="2026-05-18T00:00:00",
    )


@pytest.mark.asyncio
async def test_recall_node_disabled_short_circuits():
    retriever = MagicMock(spec=MemoryRetriever)
    retriever.search = AsyncMock(side_effect=RuntimeError("must not be called"))
    fact_store = MagicMock(spec=FactStore)
    fact_store.get = AsyncMock(side_effect=RuntimeError("must not be called"))

    result = await recall_node(
        _state_with_last_user("hello"),
        retriever,
        fact_store,
        enabled=False,
    )

    assert result == {"recalled_facts": []}
    retriever.search.assert_not_called()
    fact_store.get.assert_not_called()


@pytest.mark.asyncio
async def test_recall_node_calls_retriever_with_last_user_message():
    retriever = MagicMock(spec=MemoryRetriever)
    retriever.search = AsyncMock(return_value=[])
    fact_store = MagicMock(spec=FactStore)
    fact_store.get = AsyncMock()

    result = await recall_node(
        _state_with_last_user("what does BoBClaw use for orchestration?"),
        retriever,
        fact_store,
        enabled=True,
        top_k=3,
    )

    assert result == {"recalled_facts": []}
    retriever.search.assert_awaited_once_with(
        "what does BoBClaw use for orchestration?", top_k=3,
    )


@pytest.mark.asyncio
async def test_recall_node_empty_results_returns_empty_list():
    retriever = MagicMock(spec=MemoryRetriever)
    retriever.search = AsyncMock(return_value=[])
    fact_store = MagicMock(spec=FactStore)
    fact_store.get = AsyncMock()

    result = await recall_node(
        _state_with_last_user("test"),
        retriever,
        fact_store,
        enabled=True,
    )

    assert result == {"recalled_facts": []}
    retriever.search.assert_awaited_once()
    fact_store.get.assert_not_called()


@pytest.mark.asyncio
async def test_recall_node_propagates_retrieval_provider_error():
    retriever = MagicMock(spec=MemoryRetriever)
    retriever.search = AsyncMock(
        side_effect=RetrievalProviderError("qdrant_local", "connection refused"),
    )
    fact_store = MagicMock(spec=FactStore)

    with pytest.raises(RetrievalProviderError) as exc:
        await recall_node(
            _state_with_last_user("test"),
            retriever,
            fact_store,
            enabled=True,
        )

    assert "qdrant_local" in str(exc.value)


@pytest.mark.asyncio
async def test_recall_node_propagates_acl_violation():
    retriever = MagicMock(spec=MemoryRetriever)
    retriever.search = AsyncMock(
        side_effect=ACLViolation("bobclaw_default", "provider not allowed"),
    )
    fact_store = MagicMock(spec=FactStore)

    with pytest.raises(ACLViolation) as exc:
        await recall_node(
            _state_with_last_user("test"),
            retriever,
            fact_store,
            enabled=True,
        )

    assert "bobclaw_default" in str(exc.value)


@pytest.mark.asyncio
async def test_recall_node_returns_facts_in_state_shape():
    fact_1 = _make_fact("fact-1", {"text": "BoBClaw uses LangGraph for agent orchestration"})
    fact_2 = _make_fact("fact-2", {"text": "BoBClaw supports Kimi K2.6 as a backend"})

    chunk_1 = RetrievedChunk(
        content="BoBClaw uses LangGraph for agent orchestration",
        score=0.92,
        source_fact_id="fact-1",
        source_path=None,
        heading_path=[],
    )
    chunk_2 = RetrievedChunk(
        content="BoBClaw supports Kimi K2.6 as a backend",
        score=0.85,
        source_fact_id="fact-2",
        source_path=None,
        heading_path=[],
    )

    retriever = MagicMock(spec=MemoryRetriever)
    retriever.search = AsyncMock(return_value=[chunk_1, chunk_2])
    fact_store = MagicMock(spec=FactStore)
    fact_store.get = AsyncMock(side_effect=[fact_1, fact_2])

    result = await recall_node(
        _state_with_last_user("what does BoBClaw use?"),
        retriever,
        fact_store,
        enabled=True,
        top_k=5,
    )

    assert result == {"recalled_facts": [fact_1, fact_2]}
    retriever.search.assert_awaited_once_with(
        "what does BoBClaw use?", top_k=5,
    )
    assert fact_store.get.await_count == 2
    fact_store.get.assert_any_await("fact-1")
    fact_store.get.assert_any_await("fact-2")
