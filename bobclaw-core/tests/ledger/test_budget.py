import pytest

from core.ledger.budget import (
    reserve,
    local_breaker,
    overspend_ratio,
    should_escalate,
    reconcile_on_merge,
)
from core.ledger.types import OVERSPEND_TRIGGER


def test_reserve_grant_valid():
    """Grant request when request > 0 and request <= pool_available."""
    result = reserve(100.0, 40)
    assert result == {
        "granted": True,
        "amount": 40,
        "pool_available_after": 60.0,
    }


def test_reserve_refuse_overpool():
    """Refuse request that exceeds pool; pool unchanged."""
    result = reserve(50, 100)
    assert result == {
        "granted": False,
        "amount": 0,
        "pool_available_after": 50,
    }


def test_reserve_refuse_nonpositive_request():
    """Refuse request == 0 with pool unchanged."""
    result = reserve(100, 0)
    assert result == {
        "granted": False,
        "amount": 0,
        "pool_available_after": 100,
    }


def test_reserve_refuse_negative_request():
    """Refuse request < 0 with pool unchanged."""
    result = reserve(100, -5)
    assert result == {
        "granted": False,
        "amount": 0,
        "pool_available_after": 100,
    }


def test_local_breaker_trips_at_equal():
    """Tripped when spent == reservation."""
    result = local_breaker(50, 50)
    assert result == {"tripped": True}


def test_local_breaker_trips_above():
    """Tripped when spent > reservation."""
    result = local_breaker(60, 50)
    assert result == {"tripped": True}


def test_local_breaker_not_tripped_below():
    """Not tripped when spent < reservation."""
    result = local_breaker(40, 50)
    assert result == {"tripped": False}


def test_overspend_ratio_normal():
    """Normal ratio computation."""
    result = overspend_ratio(10, 5)
    assert result == 2.0


def test_overspend_ratio_zero_reservation():
    """Returns 0.0 when reservation is zero (no division error)."""
    result = overspend_ratio(100, 0)
    assert result == 0.0


def test_overspend_ratio_negative_reservation():
    """Returns 0.0 when reservation is negative."""
    result = overspend_ratio(100, -10)
    assert result == 0.0


def test_should_escalate_overspend_priority():
    """OVERSPEND takes precedence when ratio >= trigger, even if run_total >= run_ceiling."""
    # ratio = 2.0 >= 1.5 -> escalate OVERSPEND, run_ceiling is met but irrelevant
    result = should_escalate(
        spent=10, reservation=5, run_total=100, run_ceiling=50, trigger=OVERSPEND_TRIGGER
    )
    assert result == {"escalate": True, "reason": "OVERSPEND"}


def test_should_escalate_run_ceiling():
    """Escalate with RUN_CEILING when overspend not triggered but run_total >= run_ceiling."""
    # ratio = 3/10 = 0.3 < 1.5, run_total=20 >= 15 -> RUN_CEILING
    result = should_escalate(
        spent=3, reservation=10, run_total=20, run_ceiling=15, trigger=OVERSPEND_TRIGGER
    )
    assert result == {"escalate": True, "reason": "RUN_CEILING"}


def test_should_escalate_no_escalation():
    """No escalation when neither condition holds."""
    # ratio = 1.0 < 1.5, run_total=10 < 20
    result = should_escalate(
        spent=5, reservation=5, run_total=10, run_ceiling=20, trigger=OVERSPEND_TRIGGER
    )
    assert result == {"escalate": False, "reason": None}


def test_should_escalate_run_ceiling_not_met():
    """No escalation when overspend false and run_total < run_ceiling."""
    result = should_escalate(
        spent=1, reservation=10, run_total=5, run_ceiling=10, trigger=OVERSPEND_TRIGGER
    )
    assert result == {"escalate": False, "reason": None}


def test_should_escalate_custom_trigger():
    """Custom trigger changes overspend threshold."""
    # trigger=1.0, ratio=1.0 -> escalate OVERSPEND
    result = should_escalate(
        spent=5, reservation=5, run_total=0, run_ceiling=100, trigger=1.0
    )
    assert result == {"escalate": True, "reason": "OVERSPEND"}


def test_should_escalate_run_ceiling_boundary():
    """Escalate exactly when run_total == run_ceiling (>=)."""
    result = should_escalate(
        spent=1, reservation=10, run_total=25, run_ceiling=25, trigger=OVERSPEND_TRIGGER
    )
    assert result == {"escalate": True, "reason": "RUN_CEILING"}


def test_reconcile_on_merge_positive_return():
    """Returns positive returned amount when reservation > spent."""
    # reservation=10, spent=3 => returned = max(0, 7) = 7
    result = reconcile_on_merge(pool_available=100, reservation=10, spent=3)
    assert result == {"pool_available_after": 107, "returned": 7}


def test_reconcile_on_merge_zero_return_when_fully_spent():
    """Returns 0 when spent equals reservation."""
    result = reconcile_on_merge(pool_available=100, reservation=10, spent=10)
    assert result == {"pool_available_after": 100, "returned": 0}


def test_reconcile_on_merge_zero_return_when_over_spent():
    """Returns 0 when spent > reservation (cannot return negative)."""
    result = reconcile_on_merge(pool_available=100, reservation=10, spent=15)
    assert result == {"pool_available_after": 100, "returned": 0}


def test_reconcile_on_merge_float():
    """Works correctly with float arguments."""
    result = reconcile_on_merge(pool_available=50.5, reservation=20.25, spent=5.0)
    # returned = max(0, 20.25 - 5.0) = 15.25; pool_available_after = 50.5 + 15.25 = 65.75
    assert result == {"pool_available_after": 65.75, "returned": 15.25}
