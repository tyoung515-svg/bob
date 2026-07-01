from core.ledger.mergegate import merge_decision, is_fast_forwardable
from core.ledger.types import MergeDecision


def test_merge_decision_empty_verdicts():
    result = merge_decision([])
    assert result["decision"] == MergeDecision.REVERT.value
    assert result["reasons"] == ["default-fail: no verdicts / no evidence"]


def test_merge_decision_budget_escalated_overrides_empty():
    result = merge_decision([], budget_escalated=True)
    assert result["decision"] == MergeDecision.ESCALATE.value
    assert result["reasons"] == ["budget escalation: contested by cost"]


def test_merge_decision_budget_escalated_overrides_revert():
    verdicts = [
        {"bid_key": "k1", "verified": False, "exhausted": False}
    ]
    result = merge_decision(verdicts, budget_escalated=True)
    assert result["decision"] == MergeDecision.ESCALATE.value
    assert result["reasons"] == ["budget escalation: contested by cost"]


def test_merge_decision_budget_escalated_overrides_fast_forward():
    verdicts = [
        {"bid_key": "k1", "verified": True, "exhausted": False}
    ]
    result = merge_decision(verdicts, budget_escalated=True)
    assert result["decision"] == MergeDecision.ESCALATE.value
    assert result["reasons"] == ["budget escalation: contested by cost"]


def test_merge_decision_unverified_non_exhausted_single():
    verdicts = [
        {"bid_key": "k1", "verified": False, "exhausted": False}
    ]
    result = merge_decision(verdicts)
    assert result["decision"] == MergeDecision.REVERT.value
    assert result["reasons"] == ["unverified non-exhausted: k1"]


def test_merge_decision_unverified_non_exhausted_multiple():
    verdicts = [
        {"bid_key": "k1", "verified": False, "exhausted": False},
        {"bid_key": "k2", "verified": False, "exhausted": False},
    ]
    result = merge_decision(verdicts)
    assert result["decision"] == MergeDecision.REVERT.value
    assert result["reasons"] == [
        "unverified non-exhausted: k1",
        "unverified non-exhausted: k2",
    ]


def test_merge_decision_verified_and_exhausted_only():
    verdicts = [
        {"bid_key": "k1", "verified": True, "exhausted": False},
        {"bid_key": "k2", "verified": False, "exhausted": True},
    ]
    result = merge_decision(verdicts)
    assert result["decision"] == MergeDecision.FAST_FORWARD.value
    assert result["reasons"] == ["all criteria verified or exhausted-tagged"]


def test_merge_decision_all_verified():
    verdicts = [
        {"bid_key": "k1", "verified": True, "exhausted": False},
        {"bid_key": "k2", "verified": True, "exhausted": False},
    ]
    result = merge_decision(verdicts)
    assert result["decision"] == MergeDecision.FAST_FORWARD.value
    assert result["reasons"] == ["all criteria verified or exhausted-tagged"]


def test_merge_decision_exhausted_does_not_block():
    verdicts = [
        {"bid_key": "k1", "verified": False, "exhausted": True}
    ]
    result = merge_decision(verdicts)
    assert result["decision"] == MergeDecision.FAST_FORWARD.value
    assert result["reasons"] == ["all criteria verified or exhausted-tagged"]


def test_merge_decision_mixed_verified_exhausted():
    verdicts = [
        {"bid_key": "k1", "verified": True, "exhausted": False},
        {"bid_key": "k2", "verified": False, "exhausted": True},
        {"bid_key": "k3", "verified": True, "exhausted": True},
    ]
    result = merge_decision(verdicts)
    assert result["decision"] == MergeDecision.FAST_FORWARD.value
    assert result["reasons"] == ["all criteria verified or exhausted-tagged"]


def test_merge_decision_unverified_exhausted_does_not_cause_revert():
    # exhausted but unverified is still allowed
    verdicts = [
        {"bid_key": "k1", "verified": False, "exhausted": True}
    ]
    result = merge_decision(verdicts)
    assert result["decision"] == MergeDecision.FAST_FORWARD.value


def test_merge_decision_revert_multiple_reasons():
    verdicts = [
        {"bid_key": "a1", "verified": False, "exhausted": False},
        {"bid_key": "b2", "verified": False, "exhausted": True},  # this one is ok
        {"bid_key": "c3", "verified": False, "exhausted": False},
    ]
    result = merge_decision(verdicts)
    assert result["decision"] == MergeDecision.REVERT.value
    assert result["reasons"] == [
        "unverified non-exhausted: a1",
        "unverified non-exhausted: c3",
    ]


def test_merge_decision_empty_verdicts_with_budget_escalated_true():
    result = merge_decision([], budget_escalated=True)
    assert result["decision"] == MergeDecision.ESCALATE.value
    assert result["reasons"] == ["budget escalation: contested by cost"]


# --- is_fast_forwardable ---

def test_is_fast_forwardable_true_on_fast_forward():
    verdicts = [{"bid_key": "k1", "verified": True, "exhausted": False}]
    assert is_fast_forwardable(verdicts) is True


def test_is_fast_forwardable_false_on_revert():
    verdicts = [{"bid_key": "k1", "verified": False, "exhausted": False}]
    assert is_fast_forwardable(verdicts) is False


def test_is_fast_forwardable_false_on_escalate():
    verdicts = [{"bid_key": "k1", "verified": False, "exhausted": True}]
    assert is_fast_forwardable(verdicts, budget_escalated=True) is False


def test_is_fast_forwardable_false_on_empty():
    assert is_fast_forwardable([]) is False


def test_is_fast_forwardable_false_on_empty_with_budget_escalated():
    assert is_fast_forwardable([], budget_escalated=True) is False
