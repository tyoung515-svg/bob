"""
BoBClaw — Profiles cron scheduler daemon (P5).

A thin durable poll loop: every ``PROFILE_POLL_SECONDS`` it lists saved profiles,
fires any whose ``schedule.cron`` bucket is due (exactly once, via the SQLite
ledger), and invokes the SAME compiled graph the HTTP path uses. Opt-in, DEFAULT
OFF (``PROFILE_SCHEDULE_ENABLED``); registered as ONE ``BobClaw-Scheduler``
Task-Scheduler task by ``scripts/win/install-durability.ps1 -IncludeScheduler`` so
it survives sleep/reboot. All testable logic lives in ``core/scheduler.py`` — this
file is the wiring.

Fires are claim-then-SPAWN: ``run_tick`` claims a bucket (the exactly-once lock)
then the spawning ``invoke`` launches the graph run as a bounded background task,
so a slow council never blocks the poll loop or starves co-scheduled profiles.

Run manually (from bobclaw-core, with the venv):
    set PROFILE_SCHEDULE_ENABLED=true
    set PYTHONPATH=.
    python scripts/profile_scheduler.py
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow `python scripts/profile_scheduler.py` from the bobclaw-core dir.
_CORE_ROOT = Path(__file__).resolve().parent.parent
if str(_CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(_CORE_ROOT))

from core import teams  # noqa: E402
from core.config import config  # noqa: E402
from core.scheduler import (  # noqa: E402
    SchedulerLedger,
    build_schedule_seed,
    persist_run,
    run_tick,
    summarize_run,
)

logger = logging.getLogger("bobclaw.scheduler")


class _FireRunner:
    """Claim-then-SPAWN executor. ``run_tick`` calls :meth:`invoke` AFTER it has
    won the ledger claim; invoke only LAUNCHES the graph run as a background task
    (bounded by a semaphore) and returns immediately, so the poll loop never blocks
    on a slow council and co-scheduled profiles run concurrently. Because the claim
    already committed before invoke, a crash between claim and run still safely
    loses just that one bucket (consume-not-retry), never a duplicate."""

    def __init__(self, graph, *, max_concurrent: int, persist: bool = False,
                 default_owner: str = "admin") -> None:
        self._graph = graph
        self._sem = asyncio.Semaphore(max(1, max_concurrent))
        self._tasks: set[asyncio.Task] = set()
        self._persist = persist
        self._default_owner = default_owner

    async def invoke(self, profile: dict, schedule: dict, bucket_iso: str) -> None:
        t = asyncio.create_task(self._run_one(profile, schedule, bucket_iso))
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def _run_one(self, profile: dict, schedule: dict, bucket_iso: str) -> None:
        seed = build_schedule_seed(profile, schedule, bucket_iso)
        cfg = {
            "configurable": {"thread_id": seed["conversation_id"]},
            "recursion_limit": 50,
        }
        cid = seed["conversation_id"]
        async with self._sem:  # bound concurrent backend spend
            try:
                final = await self._graph.ainvoke(seed, cfg)
            except Exception:  # noqa: BLE001 — one bad run must not affect others
                logger.exception("scheduled run %s failed", cid)
                return
        outcome = summarize_run(final)
        if outcome["status"] == "ok":
            logger.info("scheduled run %s: %r", cid, outcome["detail"])
        else:
            # needs_approval / error / empty — surface LOUDLY (an unattended run
            # must never fail silently as an innocuous 'completed' line).
            logger.warning("scheduled run %s [%s]: %s", cid, outcome["status"],
                           outcome["detail"])

        # Surface the run in the UI (best-effort — a persistence failure must not
        # affect the run, whose answer is already in the log / L0 / checkpoint).
        if self._persist:
            try:
                from core.db import create_conversation, save_message
                owner = schedule.get("owner") or self._default_owner
                conv_id = await persist_run(
                    profile, schedule, bucket_iso, final, owner=owner,
                    create_conversation=create_conversation, save_message=save_message,
                )
                if conv_id:
                    logger.info("scheduled run %s persisted to conversation %s",
                                cid, conv_id)
            except Exception:  # noqa: BLE001
                logger.warning("scheduled run %s: persistence failed (answer still "
                               "logged)", cid, exc_info=True)

    async def drain(self) -> None:
        """Await all in-flight fires (used on shutdown / by the E2E harness)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


async def main() -> int:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not config.PROFILE_SCHEDULE_ENABLED:
        logger.info("profile scheduler disabled (PROFILE_SCHEDULE_ENABLED off); "
                    "exiting without polling")
        return 0

    poll = max(1, int(config.PROFILE_POLL_SECONDS))
    catchup = float(config.PROFILE_SCHEDULE_CATCHUP_SECONDS)
    # The one config relationship the whole no-missed-fire design rests on: a bucket
    # can be up to `poll` seconds old before the next tick sees it, so catchup MUST
    # be >= poll. Clamp + warn loudly rather than silently dropping every fire.
    if catchup < poll:
        logger.warning(
            "PROFILE_SCHEDULE_CATCHUP_SECONDS (%s) < PROFILE_POLL_SECONDS (%s): a "
            "due bucket could fall between ticks and NEVER fire. Clamping catchup to "
            "the poll interval — set it to >= 2x poll to be safe.", catchup, poll)
        catchup = float(poll)
    retention_days = max(1, int(config.PROFILE_FIRE_RETENTION_DAYS))

    # Guarded startup: a deterministic init failure (bad DB path, etc.) logs FATAL
    # and exits non-zero so the Task-Scheduler restart surfaces it, rather than an
    # unguarded crash that leaves no clear signal.
    try:
        ledger = SchedulerLedger(config.PROFILE_SCHEDULER_DB)
        await ledger.init()
        from core.graph import create_graph
        graph = await create_graph()
    except Exception:  # noqa: BLE001
        logger.exception("FATAL: scheduler startup failed (ledger/graph init); exiting")
        return 1

    # Persistence (surface output in the UI) is best-effort: if Postgres is down,
    # disable it with a warning rather than failing every fire — the answer still
    # lands in the daemon log / L0 / checkpoint.
    persist = bool(config.PROFILE_SCHEDULE_PERSIST)
    if persist:
        try:
            from core.db import init_postgres
            await init_postgres()
        except Exception:  # noqa: BLE001
            logger.warning("scheduled-run persistence disabled: Postgres unavailable "
                           "(output will be logged only)", exc_info=True)
            persist = False

    runner = _FireRunner(
        graph, max_concurrent=config.PROFILE_FIRE_CONCURRENCY,
        persist=persist, default_owner=config.PROFILE_SCHEDULE_DEFAULT_OWNER,
    )
    logger.info("profile scheduler up: poll=%ss catchup=%ss concurrency=%s persist=%s "
                "ledger=%s", poll, catchup, config.PROFILE_FIRE_CONCURRENCY,
                persist, config.PROFILE_SCHEDULER_DB)
    while True:
        now = datetime.now(timezone.utc)
        try:
            fired = await run_tick(
                now, teams.list_profiles, ledger.try_claim, runner.invoke,
                catchup_seconds=catchup,
            )
            if fired:
                logger.info("tick dispatched %d profile(s): %s", len(fired),
                            ", ".join(f"{p}@{b}" for p, b in fired))
            # Keep the ledger small: rows older than the retention horizon can never
            # be re-claimed (well past the catch-up window).
            await ledger.prune((now - timedelta(days=retention_days)).isoformat())
        except Exception:  # noqa: BLE001 — a tick failure must never kill the loop
            logger.exception("scheduler tick raised; continuing")
        await asyncio.sleep(poll)


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        logger.info("profile scheduler stopped (KeyboardInterrupt)")
