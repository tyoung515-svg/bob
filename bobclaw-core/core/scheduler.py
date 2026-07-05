"""
BoBClaw — Profiles cron scheduler (P5): testable core.

A profile carrying a ``schedule.cron`` runs unattended on a cron. This module is
the network-free, clock-injectable engine; ``scripts/profile_scheduler.py`` is the
thin daemon that loops it against the real profile store + compiled graph.

Design (per plan §Phase-5, the SPARK move-as-lock precedent):
  * **Exactly-once** is a dedicated ``scheduler_fires(profile, fire_bucket)`` table
    with an ``INSERT OR IGNORE`` on the PRIMARY KEY — atomic at the SQLite level, so
    even two pollers racing the same bucket produce exactly one claim. Its own tiny
    DB so the scheduler does NOT depend on ``MEMORY_ENABLED``.
  * **fire_bucket** is the cron-aligned timestamp (the most recent scheduled time at
    or before ``now``, via croniter). Identical for every tick inside one cron
    window, so the ledger dedups repeated ticks to a single fire.
  * **Catch-up window**: a tick only fires a bucket whose scheduled time is within
    ``catchup_seconds`` of ``now``. A long-past bucket (daemon just started / woke
    from sleep) is NOT back-fired — no surprise burst of missed runs.
  * **Claim BEFORE run** (SPARK order): a crash mid-run loses that one bucket's run
    rather than risking a duplicate; a failed graph invoke is logged, not retried
    (no retry-storm on a broken profile).
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from croniter import croniter

from core.memory._db import connection

logger = logging.getLogger(__name__)


# ── Exactly-once fire ledger ─────────────────────────────────────────────────

class SchedulerLedger:
    """The exactly-once lock: an ``INSERT OR IGNORE`` on ``(profile, fire_bucket)``.

    A successful insert (rowcount 1) means THIS caller claimed the bucket; a
    no-op insert (rowcount 0, PK already present) means another tick/process
    already claimed it. Survives crashes (durable SQLite row) and is atomic across
    processes (SQLite serializes the write), so it is correct even though in
    practice ONE dedicated daemon runs.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    async def init(self) -> None:
        """Create the ledger table (idempotent). Call once before the poll loop."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        async with connection(self._db_path) as db:
            await db.execute(
                "CREATE TABLE IF NOT EXISTS scheduler_fires ("
                "  profile     TEXT NOT NULL,"
                "  fire_bucket TEXT NOT NULL,"
                "  fired_at    TEXT NOT NULL,"
                "  PRIMARY KEY (profile, fire_bucket)"
                ")"
            )
            await db.commit()

    async def try_claim(self, profile: str, fire_bucket: str, fired_at: str) -> bool:
        """Atomically claim ``(profile, fire_bucket)``. True iff THIS call claimed it
        (won the race); False if it was already fired."""
        async with connection(self._db_path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO scheduler_fires (profile, fire_bucket, fired_at) "
                "VALUES (?, ?, ?)",
                (profile, fire_bucket, fired_at),
            )
            await db.commit()
            return cur.rowcount == 1

    async def prune(self, before_iso: str) -> int:
        """Delete fire rows whose ``fired_at`` is before ``before_iso``. A bucket
        older than the catch-up window can never be re-claimed, so old rows are pure
        history — pruning keeps the ledger small over long unattended runs without
        touching exactly-once. Returns the number of rows deleted."""
        async with connection(self._db_path) as db:
            cur = await db.execute(
                "DELETE FROM scheduler_fires WHERE fired_at < ?", (before_iso,)
            )
            await db.commit()
            return cur.rowcount if (cur.rowcount and cur.rowcount > 0) else 0


# ── Cron → fire bucket ───────────────────────────────────────────────────────

def fire_bucket_for(
    cron_expr: str, now: datetime, catchup_seconds: float
) -> Optional[datetime]:
    """The cron bucket due AT or before ``now`` and within the catch-up window.

    Returns the most recent scheduled ``datetime`` (the fire bucket) when it is
    ``0 <= now - bucket <= catchup_seconds`` old, else ``None`` (no bucket due, or
    the most recent one is stale — don't back-fire). An invalid cron expression
    logs a warning and returns ``None`` (a misconfigured profile never fires and
    never crashes the tick). ``now`` must be timezone-aware so croniter returns a
    comparable aware datetime.

    Granularity floor: this returns ONLY the single most-recent bucket per call,
    so effective cron resolution can be no finer than the poll interval — buckets
    strictly between two ticks (a sub-poll cron, or any tick gap > one cron period)
    are not back-filled. Keep cron periods >= ``PROFILE_POLL_SECONDS`` (and the
    catch-up window > the poll) so every bucket is seen. Cron is evaluated in the
    timezone of ``now`` (the daemon passes UTC).
    """
    if not cron_expr or not croniter.is_valid(cron_expr):
        logger.warning("scheduler: invalid cron %r; skipping", cron_expr)
        return None
    try:
        prev = croniter(cron_expr, now).get_prev(datetime)
    except Exception:  # noqa: BLE001 — a malformed expr that slipped past is_valid
        logger.warning("scheduler: cron %r failed to evaluate; skipping",
                       cron_expr, exc_info=True)
        return None
    age = (now - prev).total_seconds()
    if 0 <= age <= catchup_seconds:
        return prev
    return None


# ── AgentState seed (mirrors api/server.py:/api/chat) ────────────────────────

def build_schedule_seed(profile: dict, schedule: dict, bucket_iso: str) -> dict:
    """Compile the AgentState seed for a scheduled run, byte-shaped like the
    ``/api/chat`` initial_state so a scheduled turn routes identically to an HTTP
    turn. The profile pin drives the council/team path; ``schedule.face_hint`` is
    the face (advisory — route_node still decides), defaulting to ``assistant``;
    ``schedule.task`` is the turn's prompt.
    """
    face_id = schedule.get("face_hint") or "assistant"
    task = schedule.get("task") or ""
    try:
        from core.faces.registry import get_default_registry
        tools_allowed = get_default_registry().get_allowed_tools(face_id)
    except Exception:  # noqa: BLE001 — unknown face / registry hiccup → no tools
        tools_allowed = []
    return {
        "messages": [],
        "task": task,
        # Stable per-bucket conversation id so repeat fires (if any) and L0 events
        # are attributable to the schedule + its bucket.
        "conversation_id": f"sched:{profile['name']}:{bucket_iso}",
        # A scheduled turn has NO JWT identity. This is safe for today's native
        # tools (create_project fails closed on a falsy user_id; nothing else reads
        # it or acts externally) — but any FUTURE user-scoped or externally-acting
        # tool MUST fail closed on user_id=None rather than over-permit.
        "user_id": None,
        "face_id": face_id,
        "model_override": None,
        "backend_override": None,
        "team": None,
        "profile_name": profile["name"],
        "backend": "local",
        "tools_allowed": tools_allowed,
        "approval_required": False,
        "approval_response": None,
        "artifacts": [],
        "error": None,
        "project_instructions": None,
    }


def summarize_run(final: object) -> dict:
    """Classify a scheduled graph run's outcome for OBSERVABLE logging — so an
    unattended run never fails silently. Returns ``{status, detail}``:

      * ``ok``            — an assistant answer landed (detail = a snippet).
      * ``needs_approval`` — the turn tripped the dangerous-action heuristic and
        paused at the human-approval interrupt. The action was NOT executed (a
        scheduled run is unattended — there is no human to approve), and the bucket
        is consumed. Author the profile to avoid actions that require approval.
      * ``error``          — a node set ``error`` on the state.
      * ``empty``          — completed with no assistant message (e.g. an empty task).
    """
    if not isinstance(final, dict):
        return {"status": "empty", "detail": "no final state"}
    if final.get("approval_required"):
        return {"status": "needs_approval",
                "detail": "hit a human-approval gate; the action was NOT executed "
                          "(scheduled runs are unattended). Author the profile/task "
                          "to avoid actions that require approval."}
    if final.get("error"):
        return {"status": "error", "detail": str(final["error"])}
    for m in reversed(final.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "assistant" and (m.get("content") or ""):
            return {"status": "ok", "detail": m["content"][:160].replace("\n", " ")}
    return {"status": "empty", "detail": "completed with no assistant message"}


def _final_answer(final: dict) -> str:
    """The full last assistant answer (not truncated, unlike summarize_run)."""
    for m in reversed((final or {}).get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "assistant" and (m.get("content") or ""):
            return m["content"]
    return ""


async def persist_run(
    profile: dict,
    schedule: dict,
    bucket_iso: str,
    final: object,
    *,
    owner: str,
    create_conversation: Callable[..., Awaitable[dict]],
    save_message: Callable[..., Awaitable[object]],
) -> Optional[str]:
    """Persist a scheduled run to a conversation so it is VISIBLE in the desktop app
    (the same Postgres ``conversations``/``messages`` it reads) — otherwise a
    scheduled run is invisible (daemon log / L0 only).

    One conversation per fire, titled by profile + bucket, owned by ``owner`` (the
    single-user default is ``admin``): the schedule's task is the user message and
    the council answer (or, for a non-ok outcome, a short status note) is the
    assistant message. A vacuous (``empty``) run persists nothing → returns None.
    ``create_conversation`` / ``save_message`` are injected (``core.db`` functions
    in the daemon, mocks in tests). Returns the new conversation id, or None.
    """
    outcome = summarize_run(final)
    if outcome["status"] == "empty":
        return None
    conv = await create_conversation(
        user_id=owner,
        title=f"Scheduled · {profile['name']} · {bucket_iso}",
        face_id=(schedule.get("face_hint") or "assistant"),
    )
    conv_id = str(conv["id"])
    task = (schedule.get("task") or "").strip()
    if task:
        await save_message(conv_id, "user", task)
    if outcome["status"] == "ok":
        content = _final_answer(final) or outcome["detail"]
    else:
        # needs_approval / error → a short note so the run is visible + diagnosable.
        content = f"⚠ Scheduled run {outcome['status']}: {outcome['detail']}"
    await save_message(
        conv_id, "assistant", content,
        metadata={"scheduled": True, "profile": profile["name"],
                  "bucket": bucket_iso, "status": outcome["status"]},
    )
    return conv_id


# ── Per-tick logic ───────────────────────────────────────────────────────────

InvokeFn = Callable[[dict, dict, str], Awaitable[object]]


async def run_tick(
    now: datetime,
    list_profiles: Callable[[], list[dict]],
    try_claim: Callable[[str, str, str], Awaitable[bool]],
    invoke: InvokeFn,
    *,
    catchup_seconds: float,
) -> list[tuple[str, str]]:
    """One scheduler tick. Returns the list of ``(profile, fire_bucket)`` actually
    fired this tick (claimed + invoked).

    Dependencies are injected so the tick is fully testable with a fixed clock, a
    fake profile list, an in-memory/SQLite ledger, and a mock graph invoke:
      * ``list_profiles()`` — full profile envelopes (``teams.list_profiles``).
      * ``try_claim(profile, bucket_iso, fired_at) -> bool`` — the exactly-once lock.
      * ``invoke(profile, schedule, bucket_iso)`` — runs the compiled graph.

    A profile without a ``schedule.cron`` (or without a ``schedule.task``) is
    skipped. A bucket already claimed is skipped. An ``invoke`` failure is logged
    and does NOT stop the other profiles (and does NOT un-claim — the bucket is
    consumed, no retry-storm).
    """
    fired: list[tuple[str, str]] = []
    now_iso = now.isoformat()
    for profile in list_profiles():
        schedule = profile.get("schedule")
        if not isinstance(schedule, dict) or not schedule.get("cron"):
            continue
        if not schedule.get("task"):
            # A scheduled run needs a prompt; a cron with no task would burn a fire
            # on a vacuous turn every window. Skip + warn (mirrors the no-cron skip).
            logger.warning("scheduler: profile %s has a cron but no schedule.task; "
                           "skipping", profile.get("name"))
            continue
        bucket = fire_bucket_for(schedule["cron"], now, catchup_seconds)
        if bucket is None:
            continue
        bucket_iso = bucket.isoformat()
        try:
            claimed = await try_claim(profile["name"], bucket_iso, now_iso)
        except Exception:  # noqa: BLE001 — a ledger error must not sink the tick
            logger.warning("scheduler: claim failed for %s @ %s; skipping",
                           profile.get("name"), bucket_iso, exc_info=True)
            continue
        if not claimed:
            continue  # another tick/process already fired this bucket
        try:
            await invoke(profile, schedule, bucket_iso)
            fired.append((profile["name"], bucket_iso))
            logger.info("scheduler: fired profile %s for bucket %s",
                        profile["name"], bucket_iso)
        except Exception:  # noqa: BLE001 — one bad run must not stop the others
            logger.exception("scheduler: invoke failed for profile %s @ %s "
                             "(bucket consumed, not retried)",
                             profile.get("name"), bucket_iso)
    return fired
