from __future__ import annotations

"""
BoBClaw Core — the harness: the cattle-retry supervised loop + replay-from-ledger (§2.2).

Formalizes the loop the existing Send topology (``dispatch → worker → join``) embodies,
WITHOUT editing it — it reuses the same seams (the uniform hands wrap ``_send_to_backend``;
the session wraps ``ledger_slice``/``read_ledger_at``). Two behaviors are the spec heart:

  * **worker-as-cattle** — a dead worker surfaces as a ``RetryableToolCallError`` the
    supervisor re-dispatches to a FRESH worker; a worker death never escapes as an
    unhandled exception (§2.2).
  * **replay-from-ledger** — a rebooted supervisor reconstructs the committed set by
    SLICING THE LEDGER (durable, not process memory) and resumes WITHOUT re-doing
    committed work; idempotent (§2.1 reconstruct-by-slicing / §2.9 one-commit-per-trajectory).

PURE control flow — no module-global mutable state (multi-process safe).
"""

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional, Sequence

from core.harness.interfaces import (
    FatalToolCallError,
    Hands,
    RetryableToolCallError,
    Session,
    WorkerError,
)


@dataclass(frozen=True)
class TaskSpec:
    """One cattle task. ``id`` is the trajectory id — the SAME id the commit hook writes
    as the ledger event id, so a rebooted supervisor can recognize it as committed."""

    id: str
    name: str  # the hand / backend name
    input: str  # the prompt / payload


@dataclass
class TrajectoryResult:
    id: str
    status: str  # "ok" | "failed"
    output: Optional[str]
    attempts: int  # dispatches it took (1 = the first worker survived)
    error: Optional[str] = None
    replayed: bool = False  # True ⇒ reconstructed from the ledger, NOT executed this run


# ── cattle-retry: a dead worker is retried, never crashes the run ────────────────

async def supervise_task(
    task: TaskSpec, hands: Hands, *, max_retries: int = 2
) -> TrajectoryResult:
    """Dispatch ONE cattle task through the uniform hands, retrying a DEAD worker.

    A ``RetryableToolCallError`` (the §2.2 dead-worker surface) is re-dispatched to a
    fresh worker up to ``max_retries`` times. A ``FatalToolCallError`` (or any
    ``WorkerError`` with ``retryable=False``) stops immediately. A worker death NEVER
    propagates as an unhandled exception — it always returns a ``TrajectoryResult``.
    """
    last_error: Optional[BaseException] = None
    attempts = 0
    for attempt in range(max_retries + 1):
        attempts = attempt + 1
        try:
            output = await hands.execute(task.name, task.input)
        except WorkerError as exc:
            last_error = exc
            if not getattr(exc, "retryable", False):
                # Fatal: stop burning retries (bad input / unknown hand).
                return TrajectoryResult(
                    task.id, "failed", None, attempts, error=f"fatal: {exc}"
                )
            # Cattle: the worker died — re-dispatch a fresh one.
            continue
        except Exception as exc:  # noqa: BLE001 — audit r1 §2.2 hardening
            # A NON-WorkerError escaping a (possibly third-party) Hands is the contract's job to
            # map — but the supervisor must NEVER crash the run / sibling fan-out on it (§2.2
            # "a dead worker surfaces as a tool-call error", never an unhandled exception). Surface
            # it as a FAILED trajectory (visible, never silent); do NOT retry (an unexpected raw
            # error is a likely real bug, not a transient worker death — retrying just burns calls).
            # NB: asyncio.CancelledError is BaseException, so cancellation still propagates.
            return TrajectoryResult(
                task.id, "failed", None, attempts,
                error=f"unexpected: {type(exc).__name__}: {exc}",
            )
        return TrajectoryResult(task.id, "ok", str(output), attempts, error=None)
    return TrajectoryResult(
        task.id,
        "failed",
        None,
        attempts,
        error=f"exhausted_retries after {attempts}: {last_error}",
    )


# ── the fan-out: replay-aware, one-commit-per-trajectory ─────────────────────────

CommitHook = Callable[[TaskSpec, TrajectoryResult], Awaitable[None]]


async def run_fanout(
    tasks: Sequence[TaskSpec],
    hands: Hands,
    *,
    session: Optional[Session] = None,
    commit_range: Optional[str] = None,
    already_committed: Optional[set] = None,
    max_retries: int = 2,
    on_commit: Optional[CommitHook] = None,
) -> dict:
    """Run a SET of cattle tasks, replay-aware.

    The committed set is read from the LEDGER when ``session`` + ``commit_range`` are
    given (``session.committed_ids`` — durable, NOT process memory), else from an
    explicit ``already_committed`` set, else empty. A task whose id is already committed
    is NOT executed and NOT re-committed — its trajectory is reconstructed as a
    ``replayed=True`` result (§2.1 reconstruct-by-slicing).

    Fresh tasks are supervised CONCURRENTLY; then, SEQUENTIALLY in task order, each
    freshly-ok trajectory invokes ``on_commit(task, result)`` (one commit per trajectory,
    §2.9 — serialized because a single ledger branch takes one commit at a time).

    Returns ``{results, ran_ids, replayed_ids, committed_ids}``.
    """
    # Fail loud on duplicate trajectory ids (audit r1): two tasks sharing an id would share a
    # ledger event id and corrupt the replay model (double-execute / double-commit). A unique
    # trajectory id is a caller invariant, not something to silently dedup.
    ids = [t.id for t in tasks]
    if len(ids) != len(set(ids)):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(
            f"run_fanout: duplicate task ids would corrupt the trajectory ledger: {dupes}"
        )

    if session is not None and commit_range is not None:
        # The committed set is read from the DURABLE ledger (not process memory) — this is what
        # makes the SEQUENTIAL crash→reboot→replay idempotent (§2.2, the property this sprint
        # delivers). NOTE (scope, audit r2): a CONCURRENT multi-writer race (two LIVE supervisors
        # both seeing an id missing and both committing) needs an atomic ledger upsert / distributed
        # lock — explicitly out of scope here (the ledger IS the cross-process substrate; concurrent
        # write-atomicity is a follow-on, like the OpenCodeServePool→Redis move).
        committed = set(session.committed_ids(commit_range))
    else:
        committed = set(already_committed or set())

    # Partition WITHOUT executing anything: already-committed ids are reconstructed.
    fresh: list[TaskSpec] = []
    replayed_results: dict[str, TrajectoryResult] = {}
    for task in tasks:
        if task.id in committed:
            # A replayed trajectory carries output=None BY DESIGN (§2.1): its durable content
            # lives in the LEDGER, not re-materialized into the supervisor's memory. replayed=True
            # is the honest "already committed — read the ledger (session.truth_at / slice) for the
            # payload" signal; the supervisor is replay-aware, not a content cache (audit r2, rej).
            replayed_results[task.id] = TrajectoryResult(
                task.id, "ok", None, 0, error=None, replayed=True
            )
        else:
            fresh.append(task)

    # Cattle work runs concurrently (the dead-worker retry is per-task).
    fresh_results = await asyncio.gather(
        *(supervise_task(t, hands, max_retries=max_retries) for t in fresh)
    )
    by_id = {r.id: r for r in fresh_results}

    # Commit serially in task order: one commit per ok trajectory (§2.9).
    committed_ids = set(committed)
    ran_ids: set = set()
    for task in fresh:
        result = by_id[task.id]
        ran_ids.add(task.id)
        if result.status == "ok":
            if on_commit is not None:
                await on_commit(task, result)
            committed_ids.add(task.id)

    # Assemble results in the ORIGINAL task order (replayed + freshly-run).
    results: list[TrajectoryResult] = []
    for task in tasks:
        if task.id in replayed_results:
            results.append(replayed_results[task.id])
        else:
            results.append(by_id[task.id])

    return {
        "results": results,
        "ran_ids": ran_ids,
        "replayed_ids": set(replayed_results),
        "committed_ids": committed_ids,
    }


async def replay_and_resume(
    tasks: Sequence[TaskSpec],
    hands: Hands,
    session: Session,
    commit_range: str,
    *,
    max_retries: int = 2,
    on_commit: Optional[CommitHook] = None,
) -> dict:
    """The SUPERVISOR-REBOOT entrypoint (§2.2 "a crashed supervisor reboots and replays
    from the ledger").

    Reconstructs the committed set by SLICING THE LEDGER (never process memory), skips
    every already-committed trajectory, runs only the remainder, and returns the FULL
    committed set with NO duplication.

    IDEMPOTENCY: calling this twice over a growing ledger executes each task at most once;
    the 2nd call (the ledger already holds the 1st run's commits) executes NOTHING and
    returns every trajectory as ``replayed=True``.
    """
    return await run_fanout(
        tasks,
        hands,
        session=session,
        commit_range=commit_range,
        max_retries=max_retries,
        on_commit=on_commit,
    )


__all__ = [
    "TaskSpec",
    "TrajectoryResult",
    "supervise_task",
    "run_fanout",
    "replay_and_resume",
]
