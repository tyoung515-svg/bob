"""MS#4 · V1 claim-model — Slice 2 unit tests (taxonomy F4 · per-type checks D-2/F5 · F6).

PURE. Covers deterministic type classification, per-type deterministic entailment checks
(Default-FAIL, causal-downgrade F5), the provenance guard (F6), and its GUARDED integration
into the ERG (a legacy entry stays byte-identical; a v2 entry must carry claim_path).
"""
import pytest

from core.claim.v2.checks import check_causal, check_claim, check_numeric
from core.claim.v2.provenance import (
    ProvenanceError,
    claim_path_for,
    require_provenance,
    stamp_provenance,
)
from core.claim.v2.taxonomy import ClaimType, classify_claim
from core.ledger.erg import on_entailment_failure


# ── F4: deterministic classification ─────────────────────────────────────────

def test_classify_numeric_and_multi_type():
    assert classify_claim({"predicate": "weigh", "numeric_value": 5.4}) == [ClaimType.NUMERIC]
    # "reduce" is a causal lemma + a numeric value ⇒ BOTH (F4 multi-type; ALL must pass)
    assert classify_claim({"predicate": "reduces", "numeric_value": 5}) == [
        ClaimType.NUMERIC, ClaimType.CAUSAL,
    ]


def test_classify_definitional_and_existential_fallback():
    assert classify_claim({"predicate": "is", "object": "battery"}) == [ClaimType.DEFINITIONAL]
    # unknown predicate, no numeric ⇒ EXISTENTIAL is the FALLBACK (never the loosest-by-default)
    assert classify_claim({"subject": "x", "predicate": "frobnicate"}) == [ClaimType.EXISTENTIAL]


# ── D-2 / F5: per-type deterministic checks (Default-FAIL) ────────────────────

def test_numeric_check_default_fail():
    c = {"subject": "battery", "predicate": "weigh", "numeric_value": 5.4}
    assert check_numeric(c, "the battery weighs 5.40 kg")[0] is True   # 5.40 ∈ [5.4±ε]
    assert check_numeric(c, "the battery is heavy")[0] is False        # not in source → FAIL


def test_causal_downgrade_default_fail():
    c = {"subject": "agm_battery", "predicate": "prevent", "object": "sulfation"}
    # explicit causal lemma tying both entities → pass
    assert check_causal(c, "an agm battery prevents sulfation")[0] is True
    # correlation-only source with no causal relation → Default-FAIL
    assert check_causal(c, "agm battery and sulfation were both observed")[0] is False


def test_check_claim_all_types_must_pass():
    c = {"subject": "amp", "predicate": "reduces", "object": "noise", "numeric_value": 5}
    # NUMERIC + CAUSAL: a source satisfying both → pass
    ok, _ = check_claim(c, "the amp reduces noise by 5 db")
    assert ok is True
    # source satisfies causal but NOT the numeric → the conjunction FAILS
    ok2, _ = check_claim(c, "the amp reduces noise")
    assert ok2 is False


# ── F6: provenance guard ─────────────────────────────────────────────────────

def test_require_provenance_v2_vs_legacy():
    legacy = {"bid_key": "x|y|", "retry_count": 0}          # no v2 marker → allowed
    require_provenance(legacy)                               # no raise
    v2_bad = {"claim_type": "causal", "subject": "a"}       # opts into v2, no claim_path
    with pytest.raises(ProvenanceError):
        require_provenance(v2_bad)
    require_provenance(stamp_provenance({"predicate": "prevent", "object": "y", "subject": "x"}))


def test_claim_path_and_stamp_idempotent():
    c = {"subject": "b", "predicate": "weigh", "numeric_value": 5.4}
    assert claim_path_for(c) == "numeric-v2"
    s1 = stamp_provenance(c)
    assert s1["claim_schema_version"] == "claim-v2" and s1["claim_path"] == "numeric-v2"
    assert stamp_provenance(s1) == s1                        # idempotent


# ── F6 in the ERG: legacy byte-identical, v2 guarded ─────────────────────────

_LEGACY_ENTRY = {"bid_key": "s|p|", "retry_count": 0, "tried_sources": ["src1"],
                 "status": "PENDING"}


def test_erg_legacy_entry_unchanged():
    out = on_entailment_failure(_LEGACY_ENTRY, "src2")       # no v2 marker → no F6 raise
    assert out["entry"]["retry_count"] == 1
    assert out["entry"]["tried_sources"] == ["src1", "src2"]
    assert out["directive"]["action"] == "RE_BRANCH"


def test_erg_refuses_v2_entry_without_provenance():
    v2_entry = {**_LEGACY_ENTRY, "claim_type": "causal"}     # opts into v2, no claim_path
    with pytest.raises(ProvenanceError):
        on_entailment_failure(v2_entry, "src2")


def test_erg_accepts_stamped_v2_entry():
    v2_entry = stamp_provenance({**_LEGACY_ENTRY, "subject": "s", "predicate": "prevent",
                                 "object": "o"})
    out = on_entailment_failure(v2_entry, "src2")
    assert out["entry"]["retry_count"] == 1                  # provenance present → proceeds
