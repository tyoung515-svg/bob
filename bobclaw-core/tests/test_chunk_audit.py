"""Tests for the 1:10 chunk-audit reduce stage (core/chunk_audit.py).

Proves the 100→10-chunks-of-10 partition, 1:N ratio, full coverage, concurrent
auditors, deterministic ordering, and single-auditor-failure isolation.
"""
from __future__ import annotations

import asyncio

import pytest

from core.chunk_audit import chunk_audit_reduce, partition


# ── partition ───────────────────────────────────────────────────────────────────

def test_partition_100_into_10_chunks_of_10():
    chunks = partition(list(range(100)), 10)
    assert len(chunks) == 10
    assert all(len(c) == 10 for c in chunks)
    # full, ordered, no-overlap coverage of all 100
    assert [x for c in chunks for x in c] == list(range(100))


def test_partition_non_divisible_tail_kept():
    chunks = partition(list(range(95)), 10)
    assert len(chunks) == 10
    assert len(chunks[-1]) == 5
    assert sum(len(c) for c in chunks) == 95  # nothing dropped


def test_partition_edge_cases():
    assert partition([], 10) == []
    assert partition([1], 10) == [[1]]
    with pytest.raises(ValueError):
        partition([1, 2], 0)


# ── chunk_audit_reduce ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reduce_100_workers_to_10_audits_one_per_chunk():
    results = [{"idx": i, "text": f"out-{i}"} for i in range(100)]

    seen: dict[int, int] = {}

    async def audit(idx: int, chunk: list) -> dict:
        seen[idx] = len(chunk)
        return {"ok": True, "first_idx": chunk[0]["idx"]}

    audits = await chunk_audit_reduce(results, audit, chunk_size=10)

    assert len(audits) == 10                       # 1 auditor per chunk → 1:10
    assert [a["chunk_index"] for a in audits] == list(range(10))   # ordered
    assert all(a["reviewed"] == 10 for a in audits)
    assert sum(a["reviewed"] for a in audits) == 100               # full coverage
    assert seen == {i: 10 for i in range(10)}      # each auditor saw exactly its 10
    # chunk 3 reviewed workers 30..39
    assert audits[3]["verdict"]["first_idx"] == 30


@pytest.mark.asyncio
async def test_reduce_auditors_run_concurrently():
    """All 10 auditors are in flight at once (proves it's not sequential)."""
    results = list(range(100))
    in_flight = 0
    peak = 0

    async def audit(idx: int, chunk: list) -> dict:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return {"idx": idx}

    await chunk_audit_reduce(results, audit, chunk_size=10)
    assert peak == 10  # all 10 concurrent


@pytest.mark.asyncio
async def test_reduce_isolates_single_auditor_failure():
    results = list(range(100))

    async def audit(idx: int, chunk: list) -> dict:
        if idx == 4:
            raise RuntimeError("auditor 4 boom")
        return {"ok": True}

    audits = await chunk_audit_reduce(results, audit, chunk_size=10)
    assert len(audits) == 10
    bad = audits[4]
    assert bad["verdict"] is None and "boom" in bad["error"]
    # the other 9 are unaffected
    assert sum(1 for a in audits if a["verdict"] is not None) == 9


@pytest.mark.asyncio
async def test_reduce_ratio_holds_for_partial_last_chunk():
    results = list(range(95))

    async def audit(idx: int, chunk: list) -> dict:
        return {"n": len(chunk)}

    audits = await chunk_audit_reduce(results, audit, chunk_size=10)
    assert len(audits) == 10
    assert audits[-1]["reviewed"] == 5
    assert sum(a["reviewed"] for a in audits) == 95
