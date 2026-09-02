"""Regression coverage for bounded recall embedding queries.

Pure unit test: the embedder and vector provider are mocked; no model server or
Qdrant instance is contacted.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.models import RankedResults
from core.memory.retriever import (
    MemoryRetriever,
    _RECALL_QUERY_CHAR_BUDGET,
)


class _EmptyProvider:
    def query_vector(self, store_id, vector, top_k, filters, *, offset=0):
        return RankedResults(hits=[], provider_id="fake", latency_ms=0)


class _SlotResolver:
    def get(self, name):
        assert name == "embed_text"
        return object()


@pytest.mark.asyncio
async def test_oversized_recall_query_is_bounded_before_embedding():
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[[0.1, 0.2]])
    query_log = MagicMock()
    retriever = MemoryRetriever(
        embedder=embedder,
        provider=_EmptyProvider(),
        fact_store=MagicMock(),
        store_id="test",
        slot_resolver=_SlotResolver(),
        query_log=query_log,
    )
    oversized = (
        "front-task-framing "
        + "middle " * _RECALL_QUERY_CHAR_BUDGET
        + " final-user-question"
    )

    result = await retriever.search(oversized, top_k=1)

    assert result == []
    embedded = embedder.embed.await_args.args[0][0]
    assert len(embedded) == _RECALL_QUERY_CHAR_BUDGET
    assert embedded.startswith("front-task-framing ")
    assert embedded.endswith(" final-user-question")
    assert embedded != oversized


@pytest.mark.asyncio
async def test_short_recall_query_is_unchanged():
    embedder = MagicMock()
    embedder.embed = AsyncMock(return_value=[[0.1, 0.2]])
    retriever = MemoryRetriever(
        embedder=embedder,
        provider=_EmptyProvider(),
        fact_store=MagicMock(),
        store_id="test",
        slot_resolver=_SlotResolver(),
        query_log=MagicMock(),
    )

    await retriever.search("short query", top_k=1)

    embedder.embed.assert_awaited_once_with(["short query"])
