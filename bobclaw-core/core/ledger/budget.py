from __future__ import annotations

"""
BoBClaw Core — LKS git-DAG ledger: budget management (BIND-01/02, §2.9 F2).

Pure budget primitives: pool reservation, local spend breaker, overspend
escalation detection, and reconciliation on merge. All functions are
deterministic, stateless, and return JSON-serializable dictionaries or floats.
"""

from core.ledger.types import OVERSPEND_TRIGGER


def reserve(
    pool_available: int | float, request: int | float
) -> dict[str, int | float | bool]:
    """Grant or refuse a reservation request against the available pool.

    Grant if request > 0 and request <= pool_available.
    The pool is unchanged on refusal.
    """
    if request > 0 and request <= pool_available:
        return {
            "granted": True,
            "amount": request,
            "pool_available_after": pool_available - request,
        }
    return {
        "granted": False,
        "amount": 0,
        "pool_available_after": pool_available,
    }


def local_breaker(spent: int | float, reservation: int | float) -> dict[str, bool]:
    """Check if spent has met or exceeded the reservation (local trip).

    O(1), no shared polling.
    """
    return {"tripped": spent >= reservation}


def overspend_ratio(spent: int | float, reservation: int | float) -> float:
    """Return spent/reservation as a float; zero if reservation <= 0."""
    if reservation <= 0:
        return 0.0
    return spent / reservation


def should_escalate(
    spent: int | float,
    reservation: int | float,
    run_total: int | float | None = None,
    run_ceiling: int | float | None = None,
    trigger: float = OVERSPEND_TRIGGER,
) -> dict[str, bool | str | None]:
    """Determine whether to escalate based on overspend ratio or run ceiling.

    Precedence: OVERSPEND (ratio >= trigger) checked first,
    then RUN_CEILING (run_total >= run_ceiling if both provided).
    """
    ratio = overspend_ratio(spent, reservation)
    if ratio >= trigger:
        return {"escalate": True, "reason": "OVERSPEND"}
    if run_ceiling is not None and run_total is not None and run_total >= run_ceiling:
        return {"escalate": True, "reason": "RUN_CEILING"}
    return {"escalate": False, "reason": None}


def reconcile_on_merge(
    pool_available: int | float, reservation: int | float, spent: int | float
) -> dict[str, int | float]:
    """Return unspent reservation to the pool after a merge.

    ``returned = max(0, reservation - spent)``; never negative.
    """
    returned = max(0, reservation - spent)
    return {"pool_available_after": pool_available + returned, "returned": returned}
