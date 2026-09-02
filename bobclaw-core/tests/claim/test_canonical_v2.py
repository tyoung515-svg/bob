"""MS#4 · V1 claim-model — Slice 1 unit tests (v2 canonicalization).

PURE. Exercises the predicate lemmatizer + object-aware canonical key, and PINS the
Slice-1 acceptance on the frozen goldset: recall lifts off the 0.167 baseline to ≥0.50
WHILE the merge-rate gate holds at 0 (recall + precision ship together — SPEC §0).
"""
import pytest

from core.claim.v2.canonical import (
    PREDICATE_LEMMAS,
    canonical_key,
    lemmatize_predicate,
    norm_version,
)
from core.ses import extraction_recall, load_goldset, merge_rate_breakdown


# ── predicate lemmatizer (F1 table + suffix fallback) ────────────────────────

def test_lemmatize_table_morphology():
    assert lemmatize_predicate("reduces") == "reduce"
    assert lemmatize_predicate("reducing") == "reduce"
    assert lemmatize_predicate("increased") == "increase"
    assert lemmatize_predicate("REDUCES ") == "reduce"      # lower+strip first


def test_lemmatize_base_is_noop_and_fallback():
    assert lemmatize_predicate("reduce") == "reduce"        # already a base
    assert lemmatize_predicate("blocks") == "block"         # unseen → suffix fallback (-s)
    assert lemmatize_predicate("") == ""                    # never raises


# ── canonical key: object-aware + lemmatized + numeric-canonical ─────────────

def test_key_recovers_predicate_drift():
    a = {"subject": "x", "predicate": "reduce", "object": "y", "numeric_value": None}
    b = {"subject": "x", "predicate": "reduces", "object": "y", "numeric_value": None}
    assert canonical_key(a) == canonical_key(b)             # predicate morphology collapses


def test_key_is_object_aware_no_false_merge():
    a = {"subject": "cap", "predicate": "stabilize", "object": "voltage", "numeric_value": None}
    b = {"subject": "cap", "predicate": "stabilize", "object": "current", "numeric_value": None}
    assert canonical_key(a) != canonical_key(b)             # distinct object ⇒ distinct key


def test_key_numeric_canonical_and_version_tagged():
    a = {"subject": "b", "predicate": "weigh", "object": None, "numeric_value": 5.4}
    b = {"subject": "b", "predicate": "weigh", "object": None, "numeric_value": "5.40"}
    assert canonical_key(a) == canonical_key(b)             # 5.40 == 5.4
    assert canonical_key(a).startswith(norm_version() + "|")
    assert canonical_key(a, tag_version=False) == "b|weigh||5.4"


def test_norm_version_is_deterministic_and_table_sensitive():
    assert norm_version() == norm_version() and norm_version().startswith("nv1-")
    assert isinstance(PREDICATE_LEMMAS, dict) and PREDICATE_LEMMAS["prevents"] == "prevent"


# ── Slice-1 acceptance on the frozen goldset (the dual gate) ─────────────────

def test_slice1_lifts_recall_with_precision_held():
    gs = load_goldset()
    gold, extracted, distinct = gs["gold"], gs["extracted"], gs["distinct"]
    base_recall = extraction_recall(gold, extracted)["recall"]
    v2_recall = extraction_recall(gold, extracted, key=canonical_key)["recall"]
    v2_merge = merge_rate_breakdown(distinct, key=canonical_key)

    assert base_recall == pytest.approx(2 / 12)             # frozen baseline
    assert v2_recall >= 0.50                                # SPEC Slice-1 recall target
    assert v2_recall == pytest.approx(8 / 12)               # 0.667 — the measured lift
    assert v2_merge["merge_rate"] <= 0.0                    # zero false merges (gate holds)
    assert v2_merge["precision"] == pytest.approx(1.0)      # ≥0.99 precision gate
