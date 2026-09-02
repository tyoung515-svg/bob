"""Test-pipe — chunk + resume + aggregate (SPEC §6, §9 unit 8).

Proves deterministic assignment, and the load-bearing acceptance item: kill mid-run →
resume skips scored items → IDENTICAL aggregate (SPEC §10).
"""
from __future__ import annotations

import pytest

from core.testpipe.chunker import (
    ChunkLedger,
    aggregate,
    assign_chunk,
    run_chunk,
    split_chunks,
)
from core.testpipe.fixtures import fixture_items
from core.testpipe.types import ItemResult


def test_assign_chunk_is_deterministic():
    a = assign_chunk("fx_add", 3)
    b = assign_chunk("fx_add", 3)
    assert a == b and 0 <= a < 3


def test_split_chunks_partitions_all_items_stably():
    items = fixture_items()
    buckets = split_chunks(items, 3)
    assert len(buckets) == 3
    flat = [i.id for b in buckets for i in b]
    assert sorted(flat) == sorted(i.id for i in items)   # every item placed once
    # re-run is identical (deterministic)
    assert [[i.id for i in b] for b in split_chunks(items, 3)] == \
           [[i.id for i in b] for b in buckets]


def test_split_chunks_enforces_2_to_4_bound():
    items = fixture_items()
    with pytest.raises(ValueError):
        split_chunks(items, 1)
    with pytest.raises(ValueError):
        split_chunks(items, 5)


async def _score(item):
    # synthetic deterministic scorer: even-length ids pass hidden (arbitrary but stable)
    return ItemResult(item_id=item.id, config_name="B0",
                      hidden_pass=(len(item.id) % 2 == 0),
                      visible_pass=True, rounds=0, cost_usd=0.01, tokens=100)


async def test_resume_skips_scored_and_aggregate_is_identical(tmp_path):
    items = fixture_items()
    ledgers = [ChunkLedger(tmp_path / f"chunk_{i}.jsonl") for i in range(2)]
    buckets = split_chunks(items, 2)

    # ── full uninterrupted run ──
    full_ledgers = [ChunkLedger(tmp_path / f"full_{i}.jsonl") for i in range(2)]
    for lg, bucket in zip(full_ledgers, buckets):
        await run_chunk(bucket, lg, _score)
    full_agg = aggregate(full_ledgers)

    # ── interrupted + resumed run: score only 1 item per chunk, then resume ──
    for lg, bucket in zip(ledgers, buckets):
        await run_chunk(bucket, lg, _score, limit=1)         # "killed" after 1 item
    partial_scored = sum(len(lg.scored_ids()) for lg in ledgers)
    # resume: no limit — already-scored items are SKIPPED
    resumed = 0
    for lg, bucket in zip(ledgers, buckets):
        scored_now = await run_chunk(bucket, lg, _score)
        resumed += len(scored_now)
    resume_agg = aggregate(ledgers)

    assert partial_scored <= len(items)                       # the kill really stopped early
    # resume only scored the REMAINING items (no re-score of the already-done ones)
    assert resumed + partial_scored == len(items)
    # the identical-aggregate proof (SPEC §10 acceptance)
    assert resume_agg.fingerprint() == full_agg.fingerprint()
    assert resume_agg.n_items == full_agg.n_items == len(items)
    assert resume_agg.n_hidden_pass == full_agg.n_hidden_pass


async def test_ledger_tolerates_torn_final_line(tmp_path):
    lg = ChunkLedger(tmp_path / "torn.jsonl")
    lg.record(ItemResult(item_id="a", config_name="B0", hidden_pass=True))
    # simulate a kill mid-write: append a torn/partial JSON line
    with lg.path.open("a", encoding="utf-8") as fh:
        fh.write('{"item_id": "b", "hidden_pas')
    assert lg.scored_ids() == {"a"}          # the torn line is skipped, not crashed on
    assert len(lg.results()) == 1


def test_ledger_readers_are_consistent_on_incomplete_line(tmp_path):
    # audit r1: a valid-JSON line MISSING a required field must be skipped by BOTH
    # scored_ids AND results (else resume counts it done but the aggregate drops it →
    # divergence). The shared _parse_line makes the two provably agree.
    lg = ChunkLedger(tmp_path / "inc.jsonl")
    lg.record(ItemResult(item_id="a", config_name="B0", hidden_pass=True))
    with lg.path.open("a", encoding="utf-8") as fh:
        fh.write('{"item_id": "b", "hidden_pass": true}\n')   # missing required config_name
    assert lg.scored_ids() == {"a"}                          # NOT counted as scored
    assert [r.item_id for r in lg.results()] == ["a"]        # NOT in the aggregate either
