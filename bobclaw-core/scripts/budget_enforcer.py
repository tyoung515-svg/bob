"""BoBClaw — budget-enforcer daemon (Flight substrate FU1).

Polls :func:`core.flight.budget_enforcer.run_tick` over ACTIVE flights, auto-pausing any
that breach their per-flight budget (spend vs Flight.budget_usd, via the L0.4 Redis meter).
A paused flight then refuses new NAMED work at ``route_node`` (F1.3). Idempotent, so no
ledger — a re-check of an already-paused flight is a no-op.

Gated by ``FLIGHT_ENFORCE_ENABLED`` (exits 0 when off, like scripts/profile_scheduler.py's
PROFILE_SCHEDULE_ENABLED guard). Cadence = ``FLIGHT_ENFORCE_POLL_SECONDS``. Run by ONE
dedicated Task-Scheduler task (``BobClaw-BudgetEnforcer`` via install-durability.ps1
-IncludeBudgetEnforcer). Logs to the daemon's stdout (wrapper redirects to .logs).
"""
from __future__ import annotations

import asyncio
import logging
import sys

from core.config import config
from core.flight.budget_enforcer import run_tick

logger = logging.getLogger("bobclaw.budget_enforcer")


async def main() -> int:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.FLIGHT_ENFORCE_ENABLED:
        logger.info("budget enforcer disabled (FLIGHT_ENFORCE_ENABLED off); exiting without polling")
        return 0

    poll = max(1, int(config.FLIGHT_ENFORCE_POLL_SECONDS))

    # Guarded startup: a deterministic init failure (bad DB path) logs FATAL + exits
    # non-zero so the Task-Scheduler restart surfaces it (mirrors profile_scheduler.py).
    try:
        from core.flight.service import _ensure_inited

        store = await _ensure_inited()
    except Exception:
        logger.exception("FATAL: budget-enforcer startup failed (flight store init); exiting")
        return 1

    logger.info("budget enforcer up: poll=%ss db=%s", poll, config.FLIGHT_DB)
    while True:
        try:
            summary = await run_tick(store)
            if summary["paused"]:
                logger.info("tick paused %d flight(s): %s",
                            len(summary["paused"]), ", ".join(summary["paused"]))
        except Exception:  # a tick failure must never kill the loop
            logger.exception("budget-enforcer tick raised; continuing")
        await asyncio.sleep(poll)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
