from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.memory.exceptions import HopBudgetExceeded
from core.memory.interfaces import Retriever
from core.memory.models import (
    ConfidenceStub,
    Fact,
    Hit,
    RankedResults,
    RetrievedChunk,
    SlotResolution,
)
from core.memory.decay import credibility_mean
from core.memory.query_log import QueryLog
from core.memory.retriever import MemoryRetriever


@pytest.fixture
def mock_embedder():
    emb = MagicMock()
    emb.embedding_dimension = 768
    emb.embed = AsyncMock(return_value=[[0.1] * 768])
    emb.embed_query = AsyncMock(return_value=[[0.1] * 768])
    return emb


@pytest.fixture
def mock_provider():
    prov = MagicMock()
    prov.query_vector.return_value = RankedResults(
        hits=[
            Hit(id="chunk:f1:h1", score=0.85, payload={
                "text": "result content",
                "source_fact_id": "f1",
                "source_path": "fact://f1",
                "heading_path": ["H1"],
                "wikilinks": [],
            }),
        ],
        provider_id="test",
        latency_ms=10,
    )
    return prov


@pytest.fixture
def mock_fact_store():
    fact = Fact(
        fact_id="f1",
        generation_method="test",
        body={"text": "content"},
        source_event_id="evt1",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(alpha=1.0, beta=1.0, rank="normal"),
        ts="2026-01-01T00:00:00+00:00",
    )
    store = MagicMock()
    store.get = AsyncMock(return_value=fact)
    return store


@pytest.fixture
def mock_slot_resolver():
    sr = MagicMock()
    sr.get.return_value = SlotResolution(
        slot_name="embed_text",
        model="test-model",
        backend="local",
        endpoint="http://localhost:1234",
        embedding_dimension=768,
    )
    return sr


@pytest.fixture
def query_log(tmp_path: Path) -> QueryLog:
    return QueryLog(tmp_path / "query_log.jsonl")


@pytest.fixture
def retriever(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log
) -> MemoryRetriever:
    return MemoryRetriever(
        embedder=mock_embedder,
        provider=mock_provider,
        fact_store=mock_fact_store,
        store_id="test_store",
        slot_resolver=mock_slot_resolver,
        query_log=query_log,
    )


@pytest.mark.asyncio
async def test_search_hop_budget_1_returns_top_k(retriever):
    results = await retriever.search("test query", top_k=3)
    assert len(results) <= 3
    assert all(isinstance(r, RetrievedChunk) for r in results)


@pytest.mark.asyncio
async def test_search_fails_open_on_dangling_vector(
    mock_embedder, mock_provider, mock_slot_resolver, query_log,
):
    """A hit whose fact was forgotten (vector lingers, FactStore row gone) must
    be skipped, not raise L1ValidationFailed and abort the turn."""
    from core.memory.exceptions import L1ValidationFailed

    dangling_store = MagicMock()
    dangling_store.get = AsyncMock(
        side_effect=L1ValidationFailed("f1", ["fact not found"])
    )
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=dangling_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    # mock_provider returns one hit (chunk:f1:h1, source_fact_id=f1) at score 0.85.
    results = await r.search("test", top_k=3, threshold=0.0)
    assert results == []  # skipped, no raise
    dangling_store.get.assert_awaited_once_with("f1")


def _retriever(mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log):
    return MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )


@pytest.mark.asyncio
async def test_pre_fusion_threshold_drops_below_threshold(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    mock_provider.query_vector.return_value = RankedResults(
        hits=[
            Hit(id="chunk:f1:h1", score=0.1, payload={
                "text": "low score", "source_fact_id": "f1",
                "source_path": "fact://f1", "heading_path": [], "wikilinks": [],
            }),
        ],
        provider_id="test", latency_ms=10,
    )
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    results = await r.search("test", top_k=1, threshold=0.5)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_pre_fusion_threshold_zero_keeps_all(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    results = await _retriever(mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log).search("test", top_k=3, threshold=0.0)
    assert len(results) > 0


@pytest.mark.asyncio
async def test_pre_fusion_threshold_impossible_drops_all(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    results = await _retriever(mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log).search("test", top_k=3, threshold=1.5)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_credibility_multiplier_applies(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    mock_fact_store.get.return_value = Fact(
        fact_id="f1", generation_method="test", body={"text": "x"},
        source_event_id="evt1", input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(alpha=1.0, beta=1.0, rank="normal"),
        ts="2026-01-01T00:00:00+00:00",
    )
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    results = await r.search("test", top_k=1, threshold=0.0)
    expected_fused = 1.0 / 61.0
    expected_boost = expected_fused * max(0.05, 1.0 / (1.0 + 1.0))
    assert abs(results[0].boosted_score - expected_boost) < 1e-6


@pytest.mark.asyncio
async def test_credibility_floor(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    mock_fact_store.get.return_value = Fact(
        fact_id="f1", generation_method="test", body={"text": "x"},
        source_event_id="evt1", input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(alpha=1.0, beta=99.0, rank="normal"),
        ts="2026-01-01T00:00:00+00:00",
    )
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    results = await r.search("test", top_k=1, threshold=0.0)
    expected_fused = 1.0 / 61.0
    expected_boost = expected_fused * 0.05
    assert abs(results[0].boosted_score - expected_boost) < 1e-6


@pytest.mark.asyncio
async def test_deprecated_excluded_by_default(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    mock_fact_store.get.return_value = Fact(
        fact_id="f1", generation_method="test", body={"text": "x"},
        source_event_id="evt1", input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(alpha=1.0, beta=1.0, rank="deprecated"),
        ts="2026-01-01T00:00:00+00:00",
    )
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    results = await r.search("test", top_k=1, threshold=0.0)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_deprecated_included_via_filter(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    mock_fact_store.get.return_value = Fact(
        fact_id="f1", generation_method="test", body={"text": "x"},
        source_event_id="evt1", input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(alpha=1.0, beta=1.0, rank="deprecated"),
        ts="2026-01-01T00:00:00+00:00",
    )
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    results = await r.search("test", top_k=1, threshold=0.0,
                             filters={"include_deprecated": True})
    assert len(results) > 0


@pytest.mark.asyncio
async def test_hop_budget_2_wikilink_expansion(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    mock_provider.query_vector.side_effect = [
        RankedResults(
            hits=[
                Hit(id="chunk:f1:h1", score=0.9, payload={
                    "text": "main", "source_fact_id": "f1",
                    "source_path": "fact://f1", "heading_path": [],
                    "wikilinks": ["target1"],
                }),
            ],
            provider_id="test", latency_ms=5,
        ),
        RankedResults(
            hits=[
                Hit(id="chunk:f2:h1", score=0.8, payload={
                    "text": "followup", "source_fact_id": "f2",
                    "source_path": "fact://f2", "heading_path": [],
                    "wikilinks": [],
                }),
            ],
            provider_id="test", latency_ms=5,
        ),
    ]
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    results = await r.search("test", hop_budget=2, threshold=0.0, top_k=5)
    assert len(results) > 0


@pytest.mark.asyncio
async def test_hop_budget_3_raises(retriever):
    with pytest.raises(HopBudgetExceeded) as exc:
        await retriever.search("test", hop_budget=3)
    assert exc.value.requested == 3


@pytest.mark.asyncio
async def test_every_search_appends_query_log(
    retriever, query_log, tmp_path
):
    await retriever.search("test query")
    lines = (tmp_path / "query_log.jsonl").read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_embedder_uses_slot_resolver(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    await r.search("test")
    mock_slot_resolver.get.assert_called_with("embed_text")
    mock_embedder.embed_query.assert_awaited_once_with(["test"])
    assert not mock_embedder.embed.called


@pytest.mark.asyncio
async def test_decay_applied_through_retriever(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    one_year_ago = "2025-05-13T00:00:00+00:00"
    mock_fact_store.get.return_value = Fact(
        fact_id="f1", generation_method="test", body={"text": "x"},
        source_event_id="evt1", input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(
            alpha=10.0, beta=1.0, rank="normal",
            decay_class="recent_status",
            last_corroboration_ts=one_year_ago,
        ),
        ts="2026-01-01T00:00:00+00:00",
    )
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    results = await r.search("test", top_k=1, threshold=0.0)
    expected_fused = 1.0 / 61.0
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    decayed_mean = credibility_mean(
        ConfidenceStub(
            alpha=10.0, beta=1.0, rank="normal",
            decay_class="recent_status",
            last_corroboration_ts=one_year_ago,
        ),
        now,
    )
    if decayed_mean < 0.05:
        expected_boost = expected_fused * 0.05
    else:
        expected_boost = expected_fused * decayed_mean
    assert abs(results[0].boosted_score - expected_boost) < 1e-6


def test_protocol_conformance(
    mock_embedder, mock_provider, mock_fact_store, mock_slot_resolver, query_log,
):
    r = MemoryRetriever(
        embedder=mock_embedder, provider=mock_provider,
        fact_store=mock_fact_store, store_id="test_store",
        slot_resolver=mock_slot_resolver, query_log=query_log,
    )
    assert isinstance(r, Retriever)
