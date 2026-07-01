"""MS-6 — regression tests for the fleet-audit findings the manager ADOPTED (round 1).

  * §2.2 hardening: a NON-WorkerError escaping a Hands must NOT crash supervise_task /
    run_fanout's gather — the run completes, the bad trajectory is a visible failure.
  * duplicate trajectory ids fail LOUD (they would corrupt the replay ledger).

The REJECTED finding (on_commit failure should be swallowed) is intentionally NOT softened:
a durability/commit error fails loud and is recoverable-by-design via replay-from-ledger — a
test pins that an on_commit error propagates rather than being hidden.
"""
from __future__ import annotations

import pytest

from core.harness.hands import RegistryHands
from core.harness.supervisor import TaskSpec, run_fanout, supervise_task


async def test_non_workererror_does_not_escape_supervise_task():
    """A raw RuntimeError from the Hands surfaces as a failed trajectory, never an exception."""
    async def boom(inp: str) -> str:
        raise RuntimeError("raw non-WorkerError from a custom hands")

    # NOTE: bypass RegistryHands (which would map it to retryable) — call a hands that raises raw.
    class RawHands:
        async def execute(self, name, input):  # noqa: A002
            raise RuntimeError("raw non-WorkerError straight out of execute")

    result = await supervise_task(TaskSpec("t", "h", "x"), RawHands(), max_retries=2)
    assert result.status == "failed"
    assert result.attempts == 1  # not retried (unexpected error, not a known worker death)
    assert "unexpected" in result.error and "RuntimeError" in result.error


async def test_run_fanout_completes_siblings_when_one_hand_raises_raw():
    """One task's Hands raising a raw error must not abort the whole gather — siblings still run."""
    class MixedHands:
        async def execute(self, name, input):  # noqa: A002
            if name == "bad":
                raise RuntimeError("raw blow-up")
            return f"ok-{input}"

    tasks = [
        TaskSpec("t1", "good", "1"),
        TaskSpec("t2", "bad", "2"),
        TaskSpec("t3", "good", "3"),
    ]
    out = await run_fanout(tasks, MixedHands())
    by_id = {r.id: r for r in out["results"]}
    assert by_id["t1"].status == "ok"
    assert by_id["t3"].status == "ok"
    assert by_id["t2"].status == "failed"  # the raw error became a visible failure
    # The failed trajectory is NOT committed; the ok siblings are.
    assert out["committed_ids"] == {"t1", "t3"}
    assert out["ran_ids"] == {"t1", "t2", "t3"}


async def test_duplicate_task_ids_fail_loud():
    """Duplicate trajectory ids would corrupt the replay ledger -> ValueError, not silent dedup."""
    async def ok(inp: str) -> str:
        return "ok"

    hands = RegistryHands({"h": ok})
    tasks = [TaskSpec("dup", "h", "a"), TaskSpec("dup", "h", "b")]
    with pytest.raises(ValueError, match="duplicate task ids"):
        await run_fanout(tasks, hands)


async def test_on_commit_error_propagates_not_swallowed():
    """REJECTED finding 2b pinned: a commit (durability) error fails LOUD (recoverable via replay),
    it is NOT silently swallowed."""
    async def ok(inp: str) -> str:
        return "ok"

    async def failing_commit(task, result):
        raise OSError("simulated ledger write failure")

    hands = RegistryHands({"h": ok})
    tasks = [TaskSpec("t1", "h", "a")]
    with pytest.raises(OSError, match="simulated ledger write failure"):
        await run_fanout(tasks, hands, on_commit=failing_commit)
