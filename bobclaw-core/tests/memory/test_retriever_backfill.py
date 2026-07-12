"""R4 (v0.98) — recall backfill past dangling/forgotten vectors.

Regression guard for the P1 defect where the retriever fetched exactly ``top_k``
vector hits, so a run of high-ranked dangling hits (vector lingers, fact
forgotten) truncated recall below ``top_k`` even when valid lower-ranked facts
existed. The fix overfetches / pages in bounded batches, skips dangling hits, and
backfills up to ``top_k`` valid results — bounded by a documented safety cap.

Pure unit tests over a fake provider/fact-store — no live Qdrant.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock

from core.memory.exceptions import L1ValidationFailed
from core.memory.models import ConfidenceStub, Fact, Hit, RankedResults, SlotResolution
from core.memory.query_log import QueryLog
from core.memory.retriever import (
    MemoryRetriever,
    _RECALL_CANDIDATE_CAP,
    _recall_batch_size,
)


def _hit(fid: str, score: float) -> Hit:
    return Hit(
        id=f"chunk:{fid}:h",
        score=score,
        payload={
            "text": f"text for {fid}",
            "source_fact_id": fid,
            "source_path": f"fact://{fid}",
            "heading_path": [],
            "wikilinks": [],
        },
    )


def _fact(fid: str) -> Fact:
    return Fact(
        fact_id=fid,
        generation_method="extract_facts_from_event",
        body={"text": f"text for {fid}"},
        source_event_id="evt",
        input_hash="blake3:" + "a" * 64,
        confidence=ConfidenceStub(alpha=1.0, beta=1.0, rank="normal"),
        ts="2026-07-10T00:00:00+00:00",
    )


class _PagingProvider:
    """Fake provider whose hits are supplied as ordered pages keyed by offset.

    Emulates Qdrant score-ordered paging: each call returns up to ``k`` hits
    starting at ``offset`` from the flat ranked list.
    """

    def __init__(self, ranked_hits: list[Hit]) -> None:
        self._hits = ranked_hits
        self.calls: list[tuple[int, int]] = []

    def query_vector(self, store_id, vector, k=10, filters=None, *, offset=0):
        self.calls.append((offset, k))
        page = self._hits[offset : offset + k]
        return RankedResults(hits=page, provider_id="fake", latency_ms=1)


def _fact_store(valid_ids: set[str]) -> MagicMock:
    store = MagicMock()

    async def _get(fid: str) -> Fact:
        if fid in valid_ids:
            return _fact(fid)
        raise L1ValidationFailed(fid, ["fact not found"])

    store.get = AsyncMock(side_effect=_get)
    return store


def _slot_resolver() -> MagicMock:
    sr = MagicMock()
    sr.get.return_value = SlotResolution(
        slot_name="embed_text", model="m", backend="local",
        endpoint="http://localhost:1234", embedding_dimension=768,
    )
    return sr


def _embedder() -> MagicMock:
    emb = MagicMock()
    emb.embedding_dimension = 768
    # OSS retriever reads through the asymmetric seam (G-3): queries embed via
    # embed_query, documents via embed_doc.
    emb.embed_query = AsyncMock(return_value=[[0.1] * 768])
    emb.embed_doc = AsyncMock(return_value=[[0.1] * 768])
    return emb


def _retriever(provider, fact_store, tmp_path: Path) -> MemoryRetriever:
    return MemoryRetriever(
        embedder=_embedder(),
        provider=provider,
        fact_store=fact_store,
        store_id="test_store",
        slot_resolver=_slot_resolver(),
        query_log=QueryLog(tmp_path / "qlog.jsonl"),
    )


# ── batch-size / safety bound ────────────────────────────────────────────────

def test_batch_size_overfetches_but_caps_at_safety_bound():
    assert _recall_batch_size(3) == 13          # max(12, 13)
    assert _recall_batch_size(1) == 11          # max(4, 11)
    assert _recall_batch_size(10_000) == _RECALL_CANDIDATE_CAP  # capped


# ── backfill ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backfills_past_early_dangling_hits(tmp_path):
    """Top two hits are dangling; the third (valid, lower-ranked) is returned."""
    provider = _PagingProvider([
        _hit("dangling1", 0.95),
        _hit("dangling2", 0.90),
        _hit("valid3", 0.80),
    ])
    r = _retriever(provider, _fact_store({"valid3"}), tmp_path)
    results = await r.search("q", top_k=1, threshold=0.0)
    assert [c.source_fact_id for c in results] == ["valid3"]


@pytest.mark.asyncio
async def test_all_dangling_returns_empty_without_raising(tmp_path):
    provider = _PagingProvider([_hit("d1", 0.9), _hit("d2", 0.8), _hit("d3", 0.7)])
    r = _retriever(provider, _fact_store(set()), tmp_path)
    results = await r.search("q", top_k=3, threshold=0.0)
    assert results == []


@pytest.mark.asyncio
async def test_returns_multiple_valid_after_dangling(tmp_path):
    provider = _PagingProvider([
        _hit("dangling1", 0.99),
        _hit("v1", 0.80),
        _hit("v2", 0.70),
        _hit("v3", 0.60),
    ])
    r = _retriever(provider, _fact_store({"v1", "v2", "v3"}), tmp_path)
    results = await r.search("q", top_k=2, threshold=0.0)
    ids = {c.source_fact_id for c in results}
    assert ids == {"v1", "v2"}  # top-2 valid, dangling skipped


@pytest.mark.asyncio
async def test_backfill_across_pages_via_offset(tmp_path):
    """A full first page of dangling hits pages (offset) to a valid hit beyond it."""
    batch = _recall_batch_size(1)  # 11
    hits = [_hit(f"d{i}", 1.0 - i * 0.001) for i in range(batch)]  # full page, all dangling
    hits.append(_hit("valid_late", 0.5))                          # only on page 2
    provider = _PagingProvider(hits)
    r = _retriever(provider, _fact_store({"valid_late"}), tmp_path)
    results = await r.search("q", top_k=1, threshold=0.0)
    assert [c.source_fact_id for c in results] == ["valid_late"]
    # proves it actually paged: a second call at offset=batch
    assert any(offset == batch for offset, _ in provider.calls)


@pytest.mark.asyncio
async def test_paging_terminates_at_safety_cap(tmp_path):
    """A provider that never runs dry (always a full fresh page) still terminates,
    bounded by the candidate cap — no unbounded scan."""
    # Every id unique and every page full, so 'short page' / 'no fresh' never trip.
    all_dangling = [_hit(f"d{i}", 0.9) for i in range(_RECALL_CANDIDATE_CAP + 500)]
    provider = _PagingProvider(all_dangling)
    r = _retriever(provider, _fact_store(set()), tmp_path)
    results = await r.search("q", top_k=5, threshold=0.0)
    assert results == []
    scanned = sum(len(provider._hits[o : o + k]) for o, k in provider.calls)
    # never scans more than the cap (plus at most one final page overshoot)
    assert scanned <= _RECALL_CANDIDATE_CAP + _recall_batch_size(5)


@pytest.mark.asyncio
async def test_no_dangling_is_output_identical_to_single_fetch(tmp_path):
    """With no dangling hits, backfill must not change which facts come back."""
    provider = _PagingProvider([_hit("v1", 0.9), _hit("v2", 0.8), _hit("v3", 0.7)])
    r = _retriever(provider, _fact_store({"v1", "v2", "v3"}), tmp_path)
    results = await r.search("q", top_k=2, threshold=0.0)
    assert [c.source_fact_id for c in results] == ["v1", "v2"]
