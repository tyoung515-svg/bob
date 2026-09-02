"""BoBClaw — Flight substrate FU1: budget-enforcer engine (the testable tick).

One sweep = ``enforce_budget`` on every ACTIVE flight. ``enforce_budget`` is idempotent
(pausing an already-paused flight is a no-op) so — unlike the profile scheduler — NO
exactly-once ledger is needed; a re-run just re-checks. A per-flight error is logged and
skipped so one bad flight can't stall the sweep. The thin daemon
(``scripts/budget_enforcer.py``) polls this; the daemon is what the FLIGHT_ENFORCE_ENABLED
gate and cadence live on. This engine is pure control-plane over the store + spend meter.
"""
from __future__ import annotations

import logging

from core.flight.store import STATUS_ACTIVE, FlightStore
from core.flight.supervisor import enforce_budget

logger = logging.getLogger(__name__)


async def run_tick(store: FlightStore) -> dict:
    """Enforce budget on every ACTIVE flight. Returns ``{checked, paused: [flight_id, ...]}``.

    Never raises: a failed ``store.list`` yields an empty sweep; a per-flight
    ``enforce_budget`` failure is logged and skipped.
    """
    checked = 0
    paused: list[str] = []
    try:
        flights = await store.list(status=STATUS_ACTIVE)
    except Exception:
        logger.warning("budget-enforcer: list(active) failed; skipping tick", exc_info=True)
        return {"checked": 0, "paused": []}
    for f in flights:
        checked += 1
        try:
            status = await enforce_budget(store, f.flight_id)
            if status.get("paused"):
                paused.append(f.flight_id)
                logger.info(
                    "budget-enforcer: paused over-budget flight %r (%s/%s USD)",
                    f.flight_id, status.get("spent_usd"), status.get("budget_usd"),
                )
        except Exception:
            logger.warning(
                "budget-enforcer: enforce_budget failed for %r; skipping",
                f.flight_id, exc_info=True,
            )
    return {"checked": checked, "paused": paused}


__all__ = ["run_tick"]
