from __future__ import annotations

"""
BoBClaw Core — Budget BIND-01/02 runtime glue (§2.3/§2.7/§2.9 [v1.2/F2], MS-4).

PURE deterministic glue that wires the Phase-1 budget primitives
(``core.ledger.budget``) into the LIVE Send fan-out path (dispatch/worker/join +
the build branch). It REUSES — never re-derives — ``reserve`` / ``local_breaker`` /
``overspend_ratio`` / ``should_escalate`` / ``reconcile_on_merge``.

Design invariants (load-bearing):
  * No module-global MUTABLE state — every function is pure (multi-process safe;
    contrast ``core.backends._cost._DAILY_USD``).
  * Import-light — stdlib + ``core.ledger.*`` only (no ``core.nodes.execute`` /
    ``core.graph`` at module load, so unit tests stay pure under --disable-socket).
  * BIND-02 is IN-BRANCH (O(0)): a branch meters only its OWN spend against its OWN
    reservation. RUN_CEILING is a run-level aggregate decided at the MERGE
    (``reconcile_branches``), never in-branch — computing it in-branch would need the
    forbidden shared-balance poll across parallel branches (§2.9 BIND-02).
"""

import math
from typing import Optional, Sequence

from core.ledger.budget import (
    local_breaker,
    overspend_ratio,
    reconcile_on_merge,
    reserve,
    should_escalate,
)
from core.ledger.types import OVERSPEND_TRIGGER

_CHARS_PER_TOKEN = 4  # standard ~4-chars/token approximation


def approx_tokens(text: str) -> int:
    """Deterministic token measure of REAL text. ceil(len/4); "" / None -> 0."""
    if not text:
        return 0
    return math.ceil(len(text) / _CHARS_PER_TOKEN)


def _usage_tokens(usage: Optional[dict]) -> Optional[int]:
    """Real provider usage metadata (H2) when present: ``total_tokens`` (>0) else
    ``prompt_tokens + completion_tokens``. Returns ``None`` when absent/unusable so
    the caller falls back to the measured-text proxy. Pure.
    """
    if not isinstance(usage, dict) or not usage:
        return None
    total = usage.get("total_tokens")
    if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
        return int(total)
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    parts = [
        int(x)
        for x in (prompt, completion)
        if isinstance(x, (int, float)) and not isinstance(x, bool)
    ]
    if parts:
        combined = sum(parts)
        return combined if combined > 0 else 0
    return None


def measure_spend(
    messages: Optional[Sequence[dict]], response: str, usage: Optional[dict] = None
) -> int:
    """The metering load-bearer (§2.9 H2). Prefer REAL provider ``usage`` metadata when
    the seam exposes it; else measure the ACTUAL request text + response text that
    crossed the wire (a post-hoc measurement of real I/O, NOT an a-priori estimate).
    Always an int >= 0. Pure.
    """
    real = _usage_tokens(usage)
    if real is not None:
        return real
    total = 0
    for msg in messages or []:
        if isinstance(msg, dict):
            total += approx_tokens(str(msg.get("content") or ""))
    total += approx_tokens(response or "")
    return total


def budget_config(raw: Optional[dict]) -> Optional[dict]:
    """Normalize the AgentState ``budget`` trigger. ``None`` ⇒ budget OFF (the
    byte-identical guard). A present dict MUST carry a numeric ``pool`` (else -> None).
    Returns ``{pool, per_branch, run_ceiling, run_total, trigger}`` with defaults
    applied (``per_branch`` None ⇒ even split; ``trigger`` -> OVERSPEND_TRIGGER;
    ``run_total`` clamped to >= 0). Numeric types (int OR float) are preserved. Pure.
    """
    if not isinstance(raw, dict) or not raw:
        return None
    pool = raw.get("pool")
    # A negative pool is meaningless (and would feed reserve() a negative budget) — treat
    # it as budget OFF rather than silently zero-reserving every branch (audit r1 hardening).
    if not isinstance(pool, (int, float)) or isinstance(pool, bool) or pool < 0:
        return None
    per_branch = raw.get("per_branch")
    # A negative per_branch falls back to an even split (never a negative request).
    if not isinstance(per_branch, (int, float)) or isinstance(per_branch, bool) or per_branch < 0:
        per_branch = None
    run_ceiling = raw.get("run_ceiling")
    if not isinstance(run_ceiling, (int, float)) or isinstance(run_ceiling, bool):
        run_ceiling = None
    run_total = raw.get("run_total", 0)
    if not isinstance(run_total, (int, float)) or isinstance(run_total, bool):
        run_total = 0
    elif run_total < 0:
        run_total = 0
    trigger = raw.get("trigger", OVERSPEND_TRIGGER)
    if not isinstance(trigger, (int, float)) or isinstance(trigger, bool):
        trigger = OVERSPEND_TRIGGER
    return {
        "pool": pool,
        "per_branch": per_branch,
        "run_ceiling": run_ceiling,
        "run_total": run_total,
        "trigger": trigger,
    }


def plan_reservations(pool, n: int, per_branch) -> list[dict]:
    """BIND-01: reserve a per-branch sub-budget for each of ``n`` branches, sequentially
    drawing the pool via ``reserve()``. ``per_branch`` falsy ⇒ an even split
    (``pool // n``). Returns ``[{idx, requested, granted, reservation}]`` in branch
    order. A branch the pool can't cover gets ``reservation 0`` (it is immediately
    contested in-branch — §2.9 "draws from the pool only if available"; never aborts
    the fan-out). Pure (reuses ``reserve``).
    """
    if n <= 0:
        return []
    request = per_branch if per_branch else (pool // n if pool > 0 else 0)
    out: list[dict] = []
    available = pool
    for idx in range(n):
        result = reserve(available, request)
        granted = bool(result["granted"])
        out.append(
            {
                "idx": idx,
                "requested": request,
                "granted": granted,
                "reservation": result["amount"] if granted else 0,
            }
        )
        available = result["pool_available_after"]
    return out


def branch_spend_result(reservation, spent, *, trigger=OVERSPEND_TRIGGER) -> dict:
    """BIND-02 IN-BRANCH (O(0)): meter THIS branch's ``spent`` vs ITS OWN
    ``reservation`` ONLY — no shared / global / sibling read. Returns
    ``{reservation, spent, tripped, overspend_ratio, escalate, reason}`` via
    ``local_breaker`` + ``overspend_ratio`` + ``should_escalate(spent, reservation,
    trigger=trigger)``. ``escalate`` is OVERSPEND-only here (reason "OVERSPEND" | None);
    RUN_CEILING is decided at the merge, not in-branch. Pure.
    """
    breaker = local_breaker(spent, reservation)
    ratio = overspend_ratio(spent, reservation)
    escalation = should_escalate(spent, reservation, trigger=trigger)
    return {
        "reservation": reservation,
        "spent": spent,
        "tripped": bool(breaker["tripped"]),
        "overspend_ratio": ratio,
        "escalate": bool(escalation["escalate"]),
        "reason": escalation["reason"],
    }


def reconcile_branches(
    pool,
    branches: Sequence[dict],
    *,
    run_total_before=0,
    run_ceiling=None,
    trigger=OVERSPEND_TRIGGER,
) -> dict:
    """MERGE boundary (§2.9 reconcile-on-merge + §2.7 contested-by-cost surface).

    ``branches`` = the per-branch dicts the workers recorded in-branch. Reconciles
    each branch's unspent reservation back to the pool (``reconcile_on_merge``,
    sequential), aggregates run spend, and decides the THRESHOLD-GATED escalation:
      * OVERSPEND   — any branch that tripped ~150% in-branch (``b["escalate"]``).
      * RUN_CEILING — the RUN-level aggregate (``run_total_before + total_spent``)
        ``>= run_ceiling``, decided HERE where the full run spend is known (NOT
        in-branch).
    Returns the reconcile totals + an ``interrupt`` surface. Pure.
    """
    total_reserved = sum(b.get("reservation", 0) for b in branches)
    total_spent = sum(b.get("spent", 0) for b in branches)

    available = pool - total_reserved
    total_returned = 0
    for b in branches:
        rec = reconcile_on_merge(available, b.get("reservation", 0), b.get("spent", 0))
        total_returned += rec["returned"]
        available = rec["pool_available_after"]

    run_total = run_total_before + total_spent
    contested_branches = sorted(
        b.get("idx", i) for i, b in enumerate(branches) if b.get("escalate")
    )
    ceiling_hit = run_ceiling is not None and run_total >= run_ceiling
    surfaced = bool(contested_branches) or ceiling_hit
    if surfaced:
        parts = []
        if contested_branches:
            parts.append("OVERSPEND")
        if ceiling_hit:
            parts.append("RUN_CEILING")
        reason = "+".join(parts)
    else:
        reason = None

    return {
        "pool_before": pool,
        "total_reserved": total_reserved,
        "total_spent": total_spent,
        "total_returned": total_returned,
        "pool_after": available,
        "run_total_before": run_total_before,
        "run_total": run_total,
        "run_ceiling": run_ceiling,
        "branches": list(branches),
        "interrupt": {
            "surfaced": surfaced,
            "reason": reason,
            "contested_branches": contested_branches,
            "ceiling_hit": ceiling_hit,
        },
    }
