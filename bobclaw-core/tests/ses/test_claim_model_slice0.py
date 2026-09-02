"""MS#4 · V1 claim-model — Slice 0 unit tests (goldset F2 + precision F3 + decomposition).

PURE. Exercises the frozen goldset's hash-pin, the merge-rate/precision bucket, the
missed-keys decomposition, and PINS the Slice-0 baseline numbers (so Slice-1's lift is
measured against a frozen reference, not a moving one).
"""
import pytest

from core.ses import (
    DEFAULT_MERGE_RATE_THRESHOLD,
    EvalKind,
    GoldsetError,
    PrecisionError,
    extraction_recall,
    goldset_digest,
    load_goldset,
    merge_rate_breakdown,
    missed_keys_decomposition,
    precision_eval_result,
)


# ── goldset F2: frozen + hash-pinned ─────────────────────────────────────────

def test_goldset_loads_and_verifies():
    gs = load_goldset()  # raises on hash-pin mismatch
    assert {len(gs["gold"]), len(gs["extracted"])} and gs["distinct"]
    assert all("subject" in g and "predicate" in g for g in gs["gold"])


def test_goldset_hash_pin_detects_tampering():
    gs = load_goldset()
    gs["gold"].append({"subject": "x", "predicate": "y", "object": None, "numeric_value": None})
    # digest now differs from the stored pin → a strict re-verify must fail loud
    assert goldset_digest(gs) != gs["sha256"]


def test_goldset_digest_ignores_doc_but_tracks_claims():
    gs = load_goldset()
    d0 = goldset_digest(gs)
    gs["_doc"] = "edited note"          # non-payload edit → same digest
    assert goldset_digest(gs) == d0
    gs["gold"][0]["predicate"] = "MUTATED"  # payload edit → different digest
    assert goldset_digest(gs) != d0


# ── precision F3: merge-rate / false-merge gate ──────────────────────────────

def test_merge_rate_flags_object_blind_false_merge():
    gs = load_goldset()
    m = merge_rate_breakdown(gs["distinct"])
    # the object-blind baseline key merges the two capacitor-stabilize claims (voltage vs current)
    assert m["merged_groups"] == [["d3a", "d3b"]]
    assert m["merge_rate"] == pytest.approx(1 / 8)
    assert m["precision"] == pytest.approx(7 / 8)


def test_merge_rate_zero_on_fully_distinct_keys():
    distinct = [{"bid_key": "a"}, {"bid_key": "b"}, {"bid_key": "c"}]
    m = merge_rate_breakdown(distinct)
    assert m["merge_rate"] == 0.0 and m["precision"] == 1.0 and m["merged_groups"] == []


def test_merge_rate_empty_raises():
    with pytest.raises(PrecisionError):
        merge_rate_breakdown([])


def test_precision_eval_result_is_gated_regression():
    bad = merge_rate_breakdown(load_goldset()["distinct"])
    r = precision_eval_result(bad)
    assert r.kind is EvalKind.REGRESSION
    assert r.passed is False                      # merge_rate 0.125 > 0.0 threshold → gate fails
    clean = merge_rate_breakdown([{"bid_key": "a"}, {"bid_key": "b"}])
    assert precision_eval_result(clean).passed is True
    assert DEFAULT_MERGE_RATE_THRESHOLD == 0.0


# ── decomposition: sequences Slice 1 (predicate) vs Slice 3 (subject) ─────────

def test_missed_keys_decomposition_baseline():
    gs = load_goldset()
    d = missed_keys_decomposition(gs["gold"], gs["extracted"])
    assert d["counts"] == {"predicate": 6, "subject": 2, "unexplained": 2}
    assert d["n_missed"] == 10
    assert d["pct"]["predicate"] == pytest.approx(0.6)
    assert sum(d["pct"].values()) == pytest.approx(1.0)


def test_decomposition_empty_missed_is_all_zero():
    gold = [{"id": "g", "subject": "a", "predicate": "b", "object": None, "numeric_value": None}]
    d = missed_keys_decomposition(gold, gold)  # gold matches itself → nothing missed
    assert d["n_missed"] == 0 and d["counts"] == {"predicate": 0, "subject": 0, "unexplained": 0}


# ── the FROZEN Slice-0 baseline (Slice-1 must lift recall off THESE numbers) ──

def test_frozen_slice0_baseline_numbers():
    gs = load_goldset()
    recall = extraction_recall(gs["gold"], gs["extracted"])["recall"]
    merge = merge_rate_breakdown(gs["distinct"])["merge_rate"]
    assert recall == pytest.approx(2 / 12)   # 0.167 — the object-blind, non-lemmatized baseline
    assert merge == pytest.approx(1 / 8)     # 0.125 — the object-blind false-merge hole
