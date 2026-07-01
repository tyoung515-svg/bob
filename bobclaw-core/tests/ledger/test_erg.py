import pytest
from core.ledger.erg import (
    next_action,
    validate_reason,
    build_rejection_signal,
    on_entailment_failure,
)
from core.ledger.types import (
    RETRY_LIMIT,
    EXHAUSTED_TAG,
    RetryReason,
    ErgAction,
    ClaimStatus,
)


# ---------------------------------------------------------------------------
# next_action
# ---------------------------------------------------------------------------

def test_next_action_below_limit():
    for r in range(RETRY_LIMIT):
        assert next_action(r) == "RE_BRANCH", f"retry_count={r}"


def test_next_action_at_limit():
    assert next_action(RETRY_LIMIT) == "EXHAUSTED_SEARCH"


def test_next_action_above_limit():
    assert next_action(RETRY_LIMIT + 1) == "EXHAUSTED_SEARCH"
    assert next_action(RETRY_LIMIT + 100) == "EXHAUSTED_SEARCH"


def test_next_action_zero():
    assert next_action(0) == "RE_BRANCH"


def test_next_action_negative():
    # Below limit, so should be RE_BRANCH
    assert next_action(-1) == "RE_BRANCH"


# ---------------------------------------------------------------------------
# validate_reason
# ---------------------------------------------------------------------------

def test_validate_reason_none():
    assert validate_reason(None) is True


def test_validate_reason_valid_str():
    for reason in RetryReason:
        assert validate_reason(reason.value) is True, f"reason={reason.value}"


def test_validate_reason_valid_enum():
    for reason in RetryReason:
        assert validate_reason(reason) is True, f"reason={reason}"


def test_validate_reason_invalid_str():
    assert validate_reason("CUSTOM_REASON") is False
    assert validate_reason("") is False
    assert validate_reason("TEMPORAL_SCOPE_MISMATCH ") is False  # trailing space


def test_validate_reason_non_string_non_none():
    assert validate_reason(123) is False
    assert validate_reason(object()) is False


# ---------------------------------------------------------------------------
# build_rejection_signal (negative-constraint only: exact format)
# ---------------------------------------------------------------------------

def test_build_rejection_signal_empty_sources():
    bid = "test_bid"
    sources: list[str] = []
    expected = "[REJECTED: test_bid | ]"
    assert build_rejection_signal(bid, sources) == expected


def test_build_rejection_signal_one_source():
    bid = "bid1"
    sources = ["src1"]
    expected = "[REJECTED: bid1 | src1]"
    assert build_rejection_signal(bid, sources) == expected


def test_build_rejection_signal_multiple_sources():
    bid = "key"
    sources = ["a", "b", "c"]
    expected = "[REJECTED: key | a, b, c]"
    assert build_rejection_signal(bid, sources) == expected


def test_build_rejection_signal_empty_bid():
    bid = ""
    sources = ["x"]
    expected = "[REJECTED:  | x]"
    assert build_rejection_signal(bid, sources) == expected


def test_build_rejection_signal_source_with_comma():
    bid = "k"
    sources = ["a,b"]
    expected = "[REJECTED: k | a,b]"
    assert build_rejection_signal(bid, sources) == expected


# ---------------------------------------------------------------------------
# on_entailment_failure
# ---------------------------------------------------------------------------

def _make_entry(retry_count=0, tried_sources=None, status="PENDING", bid_key="bk"):
    return {
        "bid_key": bid_key,
        "retry_count": retry_count,
        "tried_sources": tried_sources if tried_sources is not None else [],
        "status": status,
    }


def test_on_entailment_failure_does_not_mutate_input():
    entry = _make_entry(retry_count=0, tried_sources=["a"], status="PENDING", bid_key="xyz")
    original = {
        "bid_key": "xyz",
        "retry_count": 0,
        "tried_sources": ["a"],
        "status": "PENDING",
    }
    result = on_entailment_failure(entry, "b", reason=None)
    assert entry == original, "input dict mutated"


def test_on_entailment_failure_re_branch_no_reason():
    entry = _make_entry(retry_count=0, tried_sources=["a"], status="PENDING", bid_key="k1")
    result = on_entailment_failure(entry, "b", reason=None)

    assert result["entry"]["retry_count"] == 1
    assert result["entry"]["tried_sources"] == ["a", "b"]
    assert result["entry"]["status"] == "PENDING"  # unchanged
    assert result["entry"]["bid_key"] == "k1"

    directive = result["directive"]
    assert directive["action"] == "RE_BRANCH"
    assert directive["bid_key"] == "k1"
    assert directive["tried_sources"] == ["a", "b"]
    expected_constraint = build_rejection_signal("k1", ["a", "b"]) + \
        " retrieve a strictly decorrelated source not in this list"
    assert directive["constraint"] == expected_constraint
    assert directive["reason"] is None


def test_on_entailment_failure_re_branch_with_valid_reason_str():
    entry = _make_entry(retry_count=0, tried_sources=[], status="PENDING", bid_key="bk2")
    result = on_entailment_failure(entry, "src1", reason=RetryReason.STALE_SOURCE.value)

    assert result["entry"]["retry_count"] == 1
    assert result["entry"]["tried_sources"] == ["src1"]

    directive = result["directive"]
    assert directive["action"] == "RE_BRANCH"
    assert directive["reason"] == "STALE_SOURCE"


def test_on_entailment_failure_re_branch_with_valid_reason_enum():
    entry = _make_entry(retry_count=0, tried_sources=[], status="PENDING", bid_key="bk3")
    result = on_entailment_failure(entry, "x", reason=RetryReason.WRONG_ENTITY)

    directive = result["directive"]
    assert directive["reason"] == "WRONG_ENTITY"


def test_on_entailment_failure_re_branch_with_invalid_reason():
    entry = _make_entry(retry_count=0, tried_sources=[], status="PENDING", bid_key="bk4")
    result = on_entailment_failure(entry, "y", reason="FREE TEXT")

    directive = result["directive"]
    assert directive["reason"] is None  # firewall: invalid reason becomes None


def test_on_entailment_failure_re_branch_dedup_new_source_already_present():
    entry = _make_entry(retry_count=0, tried_sources=["a", "b"], status="PENDING", bid_key="bk5")
    result = on_entailment_failure(entry, "a", reason=None)

    # concatenated list ["a","b","a"] deduped preserving first-seen => ["a","b"]
    assert result["entry"]["tried_sources"] == ["a", "b"]
    directive = result["directive"]
    assert directive["tried_sources"] == ["a", "b"]


def test_on_entailment_failure_re_branch_dedup_original_duplicates():
    entry = _make_entry(retry_count=0, tried_sources=["a", "b", "a"], status="PENDING", bid_key="bk6")
    result = on_entailment_failure(entry, "c", reason=None)

    # original duplicates removed: ["a","b","a","c"] -> ["a","b","c"]
    assert result["entry"]["tried_sources"] == ["a", "b", "c"]
    directive = result["directive"]
    assert directive["tried_sources"] == ["a", "b", "c"]


def test_on_entailment_failure_re_branch_orders_preserved():
    entry = _make_entry(retry_count=0, tried_sources=["c", "a", "b"], status="PENDING", bid_key="bk7")
    result = on_entailment_failure(entry, "a", reason=None)

    # concatenated: ["c","a","b","a"] deduped -> ["c","a","b"]
    assert result["entry"]["tried_sources"] == ["c", "a", "b"]


def test_on_entailment_failure_exhausted():
    entry = _make_entry(retry_count=1, tried_sources=["a"], status="PENDING", bid_key="bk8")
    result = on_entailment_failure(entry, "b", reason=None)

    assert result["entry"]["retry_count"] == 2
    assert result["entry"]["tried_sources"] == ["a", "b"]
    assert result["entry"]["status"] == ClaimStatus.UNVERIFIED_EXHAUSTED.value

    directive = result["directive"]
    assert directive["action"] == "EXHAUSTED_SEARCH"
    assert directive["status_tag"] == EXHAUSTED_TAG

    # Ensure no extra keys
    assert set(directive.keys()) == {"action", "status_tag"}


def test_on_entailment_failure_exhausted_with_reason():
    entry = _make_entry(retry_count=1, tried_sources=[], status="PENDING", bid_key="bk9")
    result = on_entailment_failure(entry, "x", reason=RetryReason.NUMERIC_MISMATCH)

    assert result["entry"]["status"] == "UNVERIFIED_EXHAUSTED"
    directive = result["directive"]
    # Even with a valid reason, exhausted branch ignores reason attribute
    assert "reason" not in directive


def test_on_entailment_failure_exhausted_does_not_mutate_directive_with_reason():
    # Ensure no reason key leaks into exhausted directive even if reason given
    entry = _make_entry(retry_count=1, tried_sources=[], status="PENDING", bid_key="k")
    result = on_entailment_failure(entry, "s", reason=RetryReason.UNSUPPORTED)
    assert "reason" not in result["directive"]


def test_on_entailment_failure_exhausted_valid_reason_ignored():
    entry = _make_entry(retry_count=1, tried_sources=[], status="PENDING", bid_key="k")
    result = on_entailment_failure(entry, "s", reason=RetryReason.WRONG_ENTITY)
    assert result["directive"] == {"action": "EXHAUSTED_SEARCH", "status_tag": EXHAUSTED_TAG}


def test_on_entailment_failure_initial_zero_retry_no_tried():
    entry = _make_entry(retry_count=0, tried_sources=[], status="PENDING", bid_key="bk10")
    result = on_entailment_failure(entry, "first", reason=None)

    assert result["entry"]["retry_count"] == 1
    assert result["entry"]["tried_sources"] == ["first"]
    assert result["directive"]["action"] == "RE_BRANCH"
    assert result["entry"]["status"] == "PENDING"


def test_on_entailment_failure_retry_count_one_becomes_exhausted():
    entry = _make_entry(retry_count=1, tried_sources=["a"], status="PENDING", bid_key="k")
    result = on_entailment_failure(entry, "b", reason=None)
    assert result["entry"]["retry_count"] == 2
    assert result["entry"]["status"] == "UNVERIFIED_EXHAUSTED"
    assert result["directive"]["action"] == "EXHAUSTED_SEARCH"


def test_on_entailment_failure_re_branch_preserves_non_pending_status():
    entry = _make_entry(retry_count=0, tried_sources=[], status="VERIFIED", bid_key="bk11")
    result = on_entailment_failure(entry, "s", reason=None)
    # Status should remain as-is (VERIFIED) per spec: "status stays as-is (PENDING)."
    # But the spec says "(PENDING)" as a common example, but the rule is "stays as-is".
    # So we preserve whatever it was.
    assert result["entry"]["status"] == "VERIFIED"
