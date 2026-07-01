"""MS2-R7 — pure unit tests for core/ses/recall.py (claim-extraction RECALL).

PURE — no model, no network, no fixtures beyond plain data. A tiny local ``_Claim`` stand-in
(with a ``bid_key`` property) exercises the Claim path WITHOUT importing core.verify.
"""
import pytest

from core.ses import (
    DEFAULT_RECALL_THRESHOLD,
    EvalKind,
    EvalResult,
    RecallError,
    claim_key,
    extraction_recall,
    recall_eval_result,
)


class _Claim:
    """A minimal stand-in for a Claim with a canonical bid_key (80.40 and 80.4 MUST collide)."""

    def __init__(self, subject, predicate, numeric_value=None):
        self._s = subject
        self._p = predicate
        self._n = numeric_value

    @property
    def bid_key(self):
        try:
            n = str(float(self._n))
        except Exception:
            n = str(self._n)
        return f"{self._s}|{self._p}|{n}"


# ── claim_key ────────────────────────────────────────────────────────────────

class TestClaimKey:
    def test_claim_object_returns_bid_key(self):
        c = _Claim("subj", "pred", 42.0)
        assert claim_key(c) == c.bid_key

    def test_claim_numeric_collision(self):
        assert claim_key(_Claim("A", "B", "80.40")) == claim_key(_Claim("A", "B", "80.4"))

    def test_dict_with_bid_key_returns_bid_key(self):
        assert claim_key({"bid_key": "my_key"}) == "my_key"

    def test_dict_with_subject_predicate_numeric_lowercased(self):
        # subject/predicate are lowered+stripped; numeric canonicalized (80.40 -> 80.4).
        assert claim_key({"subject": "X", "predicate": "Y", "numeric_value": "80.40"}) == "x|y|80.4"

    def test_dict_numeric_float_collision(self):
        d1 = {"subject": "A", "predicate": "B", "numeric_value": 80.40}
        d2 = {"subject": "A", "predicate": "B", "numeric_value": 80.4}
        assert claim_key(d1) == claim_key(d2)

    def test_dict_missing_numeric_value(self):
        assert claim_key({"subject": "A", "predicate": "B"}) == "a|b|"

    def test_bare_string_returns_itself(self):
        assert claim_key("anything") == "anything"

    def test_odd_inputs_never_raise(self):
        assert claim_key(None) == "None"
        assert claim_key(123) == "123"
        assert claim_key([1, 2]) == "[1, 2]"


# ── extraction_recall ────────────────────────────────────────────────────────

class TestExtractionRecall:
    def test_full_extraction_recall_one(self):
        gold = [_Claim("a", "b", 1)]
        r = extraction_recall(gold, [_Claim("a", "b", 1)])
        assert r["recall"] == 1.0
        assert r["n_missed"] == 0
        assert r["missed_keys"] == []

    def test_dropped_claim_lowers_recall(self):
        gold = [_Claim("a", "b", 1), _Claim("c", "d", 2)]
        r = extraction_recall(gold, [_Claim("a", "b", 1)])
        assert r["recall"] == 0.5
        assert r["n_missed"] == 1
        assert "c|d|2.0" in r["missed_keys"]

    def test_extra_extracted_not_penalized(self):
        gold = [_Claim("a", "b", 1)]
        r = extraction_recall(gold, [_Claim("a", "b", 1), _Claim("extra", "x", 99)])
        assert r["recall"] == 1.0
        assert r["n_missed"] == 0

    def test_mixed_claim_and_dict_keyed_consistently(self):
        gold = [_Claim("a", "b", 1)]
        extracted = [{"subject": "a", "predicate": "b", "numeric_value": 1}]
        r = extraction_recall(gold, extracted)
        assert r["recall"] == 1.0
        assert r["n_missed"] == 0

    def test_duplicate_gold_deduped(self):
        gold = [_Claim("a", "b", 1), _Claim("a", "b", 1)]
        r = extraction_recall(gold, [_Claim("a", "b", 1)])
        assert r["n_gold"] == 1
        assert r["recall"] == 1.0

    def test_empty_gold_raises(self):
        with pytest.raises(RecallError):
            extraction_recall([], [_Claim("a", "b", 1)])

    def test_empty_extracted_zero_recall_all_missed(self):
        gold = [_Claim("a", "b", 1), _Claim("c", "d", 2)]
        r = extraction_recall(gold, [])
        assert r["recall"] == 0.0
        assert r["n_missed"] == 2
        assert "a|b|1.0" in r["missed_keys"]

    def test_keys_sorted(self):
        gold = [_Claim("z", "a", 1), _Claim("a", "z", 2)]
        r = extraction_recall(gold, [_Claim("z", "a", 1)])
        assert r["matched_keys"] == ["z|a|1.0"]
        assert r["missed_keys"] == ["a|z|2.0"]

    def test_string_keyed(self):
        r = extraction_recall(["claim1", "claim2"], ["claim1"])
        assert r["recall"] == 0.5
        assert "claim2" in r["missed_keys"]

    def test_custom_key_function(self):
        gold = [{"subject": "s", "predicate": "p", "numeric_value": "7", "cited_source_id": "A"}]
        extracted = [{"subject": "DIFFERENT", "predicate": "words", "numeric_value": "7", "cited_source_id": "B"}]
        # keying on numeric_value only: the same number is a match despite different phrasing.
        r = extraction_recall(gold, extracted, key=lambda c: str(c.get("numeric_value")))
        assert r["recall"] == 1.0


# ── recall_eval_result ───────────────────────────────────────────────────────

class TestRecallEvalResult:
    def _bd(self, recall, missed=("z|w|2.0",)):
        n_gold = 3
        n_matched = round(recall * n_gold)
        return {
            "recall": recall, "n_gold": n_gold, "n_extracted": n_matched,
            "n_matched": n_matched, "n_missed": n_gold - n_matched,
            "matched_keys": [], "missed_keys": list(missed),
        }

    def test_perfect_recall_passes_capability(self):
        r = recall_eval_result(self._bd(1.0, missed=()))
        assert r.passed is True
        assert r.kind == EvalKind.CAPABILITY

    def test_low_recall_is_a_capability_regression(self):
        r = recall_eval_result(self._bd(2 / 3))
        assert r.passed is False  # a low recall is NOT a silent pass (§3 MS2-R7(d))
        assert r.kind == EvalKind.CAPABILITY

    def test_detail_surfaces_missed_keys(self):
        r = recall_eval_result(self._bd(0.5, missed=("z|w|2.0",)), id="x")
        assert "missed" in r.detail and "z|w|2.0" in r.detail

    def test_custom_threshold(self):
        assert recall_eval_result(self._bd(0.9, missed=("m",)), threshold=0.8).passed is True

    def test_default_threshold_is_strict(self):
        assert DEFAULT_RECALL_THRESHOLD == 1.0


# ── package export (additive; prior exports still resolve) ───────────────────

class TestPackageExports:
    def test_new_recall_names_importable(self):
        from core.ses import (  # noqa: F401
            DEFAULT_RECALL_THRESHOLD,
            RecallError,
            claim_key,
            extraction_recall,
            recall_eval_result,
        )
        assert callable(extraction_recall)
        assert callable(recall_eval_result)
        assert callable(claim_key)
        assert issubclass(RecallError, Exception)
        assert DEFAULT_RECALL_THRESHOLD == 1.0

    def test_prior_exports_still_resolve(self):
        # no-regression on the MS-5 package surface (the additive export changed nothing else).
        from core.ses import (  # noqa: F401
            DEFAULT_VOLATILE_KEYS,
            EvalCase,
            EvalKind,
            EvalResult,
            Label,
            LabeledItem,
            ScoreBreakdown,
            SesError,
            SuiteScore,
            behavioral_fingerprint,
            extract_decisions,
            false_pass_rate,
            run_evals,
            score_results,
            strip_volatile,
        )
        assert callable(false_pass_rate)
        assert callable(score_results)
        assert callable(run_evals)
        assert callable(behavioral_fingerprint)
        for cls in (EvalCase, EvalKind, EvalResult, Label, LabeledItem, ScoreBreakdown, SuiteScore):
            assert isinstance(cls, type)

    def test_recall_error_is_a_ses_error(self):
        from core.ses import RecallError, SesError
        assert issubclass(RecallError, SesError)
