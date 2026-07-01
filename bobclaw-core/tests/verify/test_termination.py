from __future__ import annotations

import pytest
from core.verify.termination import (
    Criterion,
    default_fail_criteria,
    criterion_from_outcome,
    termination_decision,
    is_complete,
    could_not_verify,
)
from core.ledger.types import EXHAUSTED_TAG


class TestDefaultFailCriteria:
    def test_multiple_keys(self):
        keys = ["a", "b"]
        criteria = default_fail_criteria(keys)
        assert len(criteria) == 2
        for c in criteria:
            assert c.verified is False
            assert c.exhausted is False
            assert c.tag == "U"
        assert criteria[0].key == "a"
        assert criteria[1].key == "b"

    def test_empty_iterable(self):
        criteria = default_fail_criteria([])
        assert criteria == []


class TestTerminationDecision:
    def test_all_unverified_non_exhausted(self):
        criteria = [
            Criterion(key="a", verified=False, exhausted=False),
            Criterion(key="b", verified=False, exhausted=False),
        ]
        result = termination_decision(criteria)
        assert result["decision"] == "REVERT"

    def test_empty_criteria(self):
        result = termination_decision([])
        assert result["decision"] == "REVERT"

    def test_verified_and_exhausted(self):
        criteria = [
            Criterion(key="a", verified=True, exhausted=False),
            Criterion(key="b", verified=False, exhausted=True),
        ]
        result = termination_decision(criteria)
        assert result["decision"] == "FAST_FORWARD"

    def test_lone_unverified_non_exhausted(self):
        criteria = [
            Criterion(key="a", verified=True, exhausted=False),
            Criterion(key="b", verified=False, exhausted=False),
        ]
        result = termination_decision(criteria)
        assert result["decision"] == "REVERT"

    def test_budget_escalated(self):
        criteria = [
            Criterion(key="a", verified=True, exhausted=False),
        ]
        result = termination_decision(criteria, budget_escalated=True)
        assert result["decision"] == "ESCALATE"

    def test_budget_escalated_with_exhausted(self):
        criteria = [
            Criterion(key="a", verified=False, exhausted=True),
        ]
        result = termination_decision(criteria, budget_escalated=True)
        assert result["decision"] == "ESCALATE"


class TestIsComplete:
    def test_all_unverified_non_exhausted(self):
        criteria = [
            Criterion(key="a", verified=False, exhausted=False),
            Criterion(key="b", verified=False, exhausted=False),
        ]
        assert is_complete(criteria) is False

    def test_empty_criteria(self):
        assert is_complete([]) is False

    def test_verified_and_exhausted(self):
        criteria = [
            Criterion(key="a", verified=True, exhausted=False),
            Criterion(key="b", verified=False, exhausted=True),
        ]
        assert is_complete(criteria) is True

    def test_mixed_with_one_unverified(self):
        criteria = [
            Criterion(key="a", verified=True, exhausted=False),
            Criterion(key="b", verified=False, exhausted=False),
        ]
        assert is_complete(criteria) is False

    def test_budget_escalated(self):
        criteria = [
            Criterion(key="a", verified=True, exhausted=False),
        ]
        assert is_complete(criteria, budget_escalated=True) is False


class TestCriterionFromOutcome:
    def test_verified_outcome(self):
        outcome = {
            "bid_key": "claim1",
            "verified": True,
            "exhausted": False,
            "final_tag": "PV",
        }
        c = criterion_from_outcome(outcome)
        assert c.key == "claim1"
        assert c.verified is True
        assert c.exhausted is False
        assert c.tag == "PV"

    def test_exhausted_outcome(self):
        outcome = {
            "bid_key": "claim2",
            "verified": False,
            "exhausted": True,
            "final_tag": "U",
        }
        c = criterion_from_outcome(outcome)
        assert c.key == "claim2"
        assert c.verified is False
        assert c.exhausted is True
        assert c.tag == "U"

    def test_exhausted_outcome_complete(self):
        outcome = {
            "bid_key": "claim2",
            "verified": False,
            "exhausted": True,
            "final_tag": "U",
        }
        c = criterion_from_outcome(outcome)
        # is_complete should accept exhausted criteria
        assert is_complete([c]) is True

    def test_default_tag(self):
        # If final_tag is missing, should default to "U"
        outcome = {
            "bid_key": "claim3",
            "verified": False,
            "exhausted": False,
        }
        c = criterion_from_outcome(outcome)
        assert c.tag == "U"


class TestCouldNotVerify:
    def test_mixed_criteria(self):
        criteria = [
            Criterion(key="a", verified=True, exhausted=False, tag="PV"),
            Criterion(key="b", verified=False, exhausted=True, tag="U"),
            Criterion(key="c", verified=False, exhausted=False, tag="U"),
        ]
        unverified = could_not_verify(criteria)
        assert len(unverified) == 2
        keys = {c.key for c in unverified}
        assert keys == {"b", "c"}
        for c in unverified:
            assert c.verified is False

    def test_all_verified(self):
        criteria = [
            Criterion(key="a", verified=True, exhausted=False, tag="PV"),
            Criterion(key="b", verified=True, exhausted=False, tag="VS"),
        ]
        assert could_not_verify(criteria) == []

    def test_all_exhausted(self):
        criteria = [
            Criterion(key="a", verified=False, exhausted=True, tag="U"),
        ]
        unverified = could_not_verify(criteria)
        assert len(unverified) == 1
        assert unverified[0].key == "a"

    def test_empty_criteria(self):
        assert could_not_verify([]) == []


class TestCriterionToVerdict:
    def test_verified_false(self):
        c = Criterion(key="x", verified=False, exhausted=False)
        assert c.to_verdict() == {"bid_key": "x", "verified": False, "exhausted": False}

    def test_verified_true(self):
        c = Criterion(key="y", verified=True, exhausted=True)
        assert c.to_verdict() == {"bid_key": "y", "verified": True, "exhausted": True}

    def test_exhausted_only(self):
        c = Criterion(key="z", verified=False, exhausted=True)
        assert c.to_verdict() == {"bid_key": "z", "verified": False, "exhausted": True}
