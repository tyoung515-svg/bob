"""Regression tests for the round-1 fleet diff-audit findings (issues the original
worker-authored tests missed). See tasks/2026-06-29-lks-git-dag-ledger/audit_fleet_result_r1.txt."""
from __future__ import annotations

from core.ledger.bidkey import bid_key, predicate_lemma
from core.ledger.commits import canonical_commit, commit_hash, squash_trajectory
from core.ledger.mergegate import merge_decision
from core.ledger.types import MergeDecision


# ── audit r2 §1 — bid_key field-boundary collision via a literal "|" ────────────
def test_bid_key_pipe_in_field_does_not_collide():
    # ("a|b","c") and ("a","b|c") must NOT hash to the same key (json-serialized triple)
    assert bid_key("a|b", "c") != bid_key("a", "b|c")


def test_bid_key_addressed_collapses_to_address():
    # round-2 worker claimed addressed->addres (false); confirm it collapses to address
    assert bid_key("server", "address") == bid_key("server", "addressed")


# ── audit §1 — predicate_lemma must not strip a non-plural trailing 's' ──────────
def test_predicate_lemma_keeps_non_plural_s():
    for word in ("process", "address", "status", "analysis", "basis", "chaos"):
        assert predicate_lemma(word) == word  # never a non-word stem like "proces"


def test_predicate_lemma_process_consistent_with_inflections():
    # the bug: process->proces but processed->process (under-collapse). Must now agree.
    assert predicate_lemma("process") == predicate_lemma("processed") == predicate_lemma("processing")


def test_predicate_lemma_real_plural_still_strips():
    assert predicate_lemma("models") == "model"
    assert predicate_lemma("scores") == "score"  # via the synonym map


# ── audit §4 — merge_decision is fail-closed on malformed/incomplete verdicts ────
def test_merge_decision_missing_keys_reverts():
    assert merge_decision([{"bid_key": "k1"}])["decision"] == MergeDecision.REVERT.value


def test_merge_decision_none_verified_reverts():
    d = merge_decision([{"bid_key": "k1", "verified": None, "exhausted": None}])
    assert d["decision"] == MergeDecision.REVERT.value


def test_merge_decision_no_keyerror_on_missing_bid_key():
    d = merge_decision([{"verified": False, "exhausted": False}])  # must not raise
    assert d["decision"] == MergeDecision.REVERT.value


def test_merge_decision_wellformed_unchanged():
    assert merge_decision([{"bid_key": "k", "verified": True, "exhausted": False}])["decision"] \
        == MergeDecision.FAST_FORWARD.value
    assert merge_decision([{"bid_key": "k", "verified": False, "exhausted": True}])["decision"] \
        == MergeDecision.FAST_FORWARD.value


# ── audit §5 — commits: None vs real-falsy coercion + None-safe parents/claims ───
def test_canonical_commit_none_field_is_empty_not_literal():
    assert canonical_commit({"trajectory_id": None})["trajectory_id"] == ""
    assert canonical_commit({"boundary_kind": None})["boundary_kind"] == ""
    assert canonical_commit({"message": None})["message"] == ""


def test_canonical_commit_zero_trajectory_distinct_from_missing():
    assert canonical_commit({"trajectory_id": 0})["trajectory_id"] == "0"
    assert commit_hash({"trajectory_id": 0}) != commit_hash({"trajectory_id": None})


def test_canonical_commit_none_parent_does_not_crash():
    assert canonical_commit({"parents": [None, "a", None, "b"]})["parents"] == ["a", "b"]


def test_squash_none_parent_and_message_safe():
    out = squash_trajectory(
        [
            {"parents": [None, "p1"], "message": None, "claims": ["c1", None]},
            {"message": "second", "claims": ["c1", "c2"]},
        ],
        trajectory_id="t1",
    )
    assert out["parents"] == ["p1"]
    assert out["message"] == "second"
    assert out["claims"] == ["c1", "c2"]
