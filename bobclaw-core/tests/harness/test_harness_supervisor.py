"""MS-6 — INDEPENDENT verification of the cattle-retry + replay-from-ledger semantics.

deepseek-authored (assertions preserved verbatim); the manager fixed the worker's
``RegistryHands`` callable arity — ``RegistryHands.execute`` dispatches ``fn(input)`` (a
SINGLE arg), so the registry callables take ``(input)``, not ``(name, input)``.
"""
import json
import subprocess
from pathlib import Path

from core.harness.supervisor import (
    TaskSpec,
    TrajectoryResult,
    supervise_task,
    run_fanout,
    replay_and_resume,
)
from core.harness.hands import RegistryHands
from core.harness.interfaces import RetryableToolCallError, FatalToolCallError
from core.harness.session import LedgerSession


# ---------------------------------------------------------------------------
# Helper: initialise a git repository with a base commit
# ---------------------------------------------------------------------------
def _init_repo(path: Path) -> str:
    """Create a minimal git repo with an empty ledger/events.jsonl and return the base commit SHA."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)

    ledger_dir = path / "ledger"
    ledger_dir.mkdir()
    events_file = ledger_dir / "events.jsonl"
    events_file.write_text("")   # empty file

    subprocess.run(["git", "add", str(ledger_dir)], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Helper: write one event line and commit it
# ---------------------------------------------------------------------------
async def _commit_event(repo_path: Path, task_id: str, output: str, status: str = "ok") -> None:
    event = {"id": task_id, "status": status, "output": output, "replayed": False}
    events_file = repo_path / "ledger" / "events.jsonl"
    with open(events_file, "a") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")
    subprocess.run(["git", "add", str(events_file)], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"trajectory {task_id}"], cwd=repo_path, check=True, capture_output=True
    )


# ===================================================================
# 1.  supervise_task – cattle-retry semantics
# ===================================================================

async def test_supervise_task_retryable_then_succeeds() -> None:
    """First call raises RetryableToolCallError, second call returns 'done'."""
    counter = 0

    async def fn(inp: str) -> str:
        nonlocal counter
        counter += 1
        if counter == 1:
            raise RetryableToolCallError("first failure")
        return "done"

    hands = RegistryHands({"test": fn})
    task = TaskSpec("id1", "test", "input")
    result = await supervise_task(task, hands)

    assert result.status == "ok"
    assert result.attempts == 2
    assert result.output == "done"


async def test_supervise_task_always_retryable_fails() -> None:
    """Every call raises RetryableToolCallError – supervisor exhausts retries."""
    counter = 0

    async def fn(inp: str) -> str:
        nonlocal counter
        counter += 1
        raise RetryableToolCallError("always fail")

    hands = RegistryHands({"test": fn})
    task = TaskSpec("id2", "test", "input")
    result = await supervise_task(task, hands)

    assert result.status == "failed"
    assert result.attempts == 3   # 1 original + max_retries=2 default
    assert "retry" in result.error or "exhausted" in result.error
    assert counter == 3           # the fresh worker was re-dispatched each retry


async def test_supervise_task_fatal_stops_immediately() -> None:
    """FatalToolCallError stops after one attempt, no retry."""
    async def fn(inp: str) -> str:
        raise FatalToolCallError("permanent")

    hands = RegistryHands({"test": fn})
    task = TaskSpec("id3", "test", "input")
    result = await supervise_task(task, hands)

    assert result.status == "failed"
    assert result.attempts == 1


# ===================================================================
# 2.  run_fanout – fresh run, no session
# ===================================================================

async def test_run_fanout_fresh() -> None:
    """3 tasks all succeed – on_commit called in order, ran_ids equals all, replayed empty."""
    hand_call_count = 0

    async def hand(inp: str) -> str:
        nonlocal hand_call_count
        hand_call_count += 1
        return f"ok-{inp}"

    hands = RegistryHands({"a": hand, "b": hand, "c": hand})
    tasks = [
        TaskSpec("t1", "a", "one"),
        TaskSpec("t2", "b", "two"),
        TaskSpec("t3", "c", "three"),
    ]

    commit_order = []

    async def on_commit(task: TaskSpec, result: TrajectoryResult):
        commit_order.append(task.id)

    outcome = await run_fanout(tasks, hands, on_commit=on_commit)

    assert all(r.status == "ok" for r in outcome["results"])
    assert outcome["ran_ids"] == {"t1", "t2", "t3"}
    assert outcome["replayed_ids"] == set()
    assert outcome["committed_ids"] == {"t1", "t2", "t3"}
    assert commit_order == ["t1", "t2", "t3"]
    assert hand_call_count == 3


# ===================================================================
# 3.  run_fanout – with already_committed set
# ===================================================================

async def test_run_fanout_with_already_committed() -> None:
    """Tasks in already_committed are reconstructed with replayed=True, hand never called."""
    call_counts = {}

    async def hand_x(inp: str) -> str:
        call_counts["x"] = call_counts.get("x", 0) + 1
        return "done"

    async def hand_y(inp: str) -> str:
        call_counts["y"] = call_counts.get("y", 0) + 1
        return "done"

    hands = RegistryHands({"x": hand_x, "y": hand_y})
    tasks = [
        TaskSpec("t1", "x", "first"),
        TaskSpec("t2", "y", "second"),
    ]

    commit_called_for = []

    async def on_commit(task: TaskSpec, result: TrajectoryResult):
        commit_called_for.append(task.id)

    outcome = await run_fanout(
        tasks, hands,
        already_committed={"t1"},
        on_commit=on_commit,
    )

    # t1 must be replayed, hand not called
    assert outcome["results"][0].replayed is True
    assert outcome["results"][1].replayed is False
    assert call_counts.get("x", 0) == 0   # t1 hand never called
    assert call_counts.get("y", 0) == 1   # t2 hand called once
    assert commit_called_for == ["t2"]
    assert outcome["ran_ids"] == {"t2"}
    assert outcome["replayed_ids"] == {"t1"}
    assert outcome["committed_ids"] == {"t1", "t2"}


# ===================================================================
# 4.  replay_and_resume – idempotency over a real temp git ledger
# ===================================================================

async def test_replay_and_resume_idempotency(tmp_path: Path) -> None:
    """Calls replay_and_resume twice – only missing tasks executed; second call executes nothing."""
    repo_path = tmp_path / "repo"
    base_sha = _init_repo(repo_path)
    commit_range = f"{base_sha}..HEAD"

    # ---------- Hands that count calls ----------
    call_counts: dict[str, int] = {}

    async def hand(inp: str) -> str:
        call_counts["worker"] = call_counts.get("worker", 0) + 1
        return f"output-{inp}"

    hands = RegistryHands({"worker": hand})

    tasks = [
        TaskSpec("t1", "worker", "1"),
        TaskSpec("t2", "worker", "2"),
        TaskSpec("t3", "worker", "3"),
        TaskSpec("t4", "worker", "4"),
    ]

    # ---------- Seed: run only the first two tasks ----------
    async def seed_on_commit(task: TaskSpec, result: TrajectoryResult) -> None:
        await _commit_event(repo_path, task.id, result.output, result.status)

    seed_result = await run_fanout(
        tasks[:2], hands,
        session=None,
        on_commit=seed_on_commit,
    )
    assert seed_result["committed_ids"] == {"t1", "t2"}
    assert call_counts.get("worker", 0) == 2

    # ---------- First replay_and_resume – should run t3,t4 ----------
    call_counts.clear()
    events_written = []

    async def resume_on_commit(task: TaskSpec, result: TrajectoryResult) -> None:
        events_written.append(task.id)
        await _commit_event(repo_path, task.id, result.output, result.status)

    session = LedgerSession(str(repo_path))

    outcome1 = await replay_and_resume(
        tasks, hands, session, commit_range,
        on_commit=resume_on_commit,
    )

    # t1,t2 already in ledger – hands not called for them
    assert outcome1["replayed_ids"] == {"t1", "t2"}
    assert outcome1["ran_ids"] == {"t3", "t4"}
    assert outcome1["committed_ids"] == {"t1", "t2", "t3", "t4"}

    # Hands called only for t3,t4
    assert call_counts.get("worker", 0) == 2

    # Only t3,t4 triggered on_commit
    assert set(events_written) == {"t3", "t4"}

    # ---------- Second replay_and_resume – nothing to do ----------
    call_counts.clear()
    events_written.clear()

    outcome2 = await replay_and_resume(
        tasks, hands, session, commit_range,
        on_commit=resume_on_commit,
    )

    assert call_counts.get("worker", 0) == 0
    assert outcome2["replayed_ids"] == {"t1", "t2", "t3", "t4"}
    assert outcome2["ran_ids"] == set()
    assert outcome2["committed_ids"] == {"t1", "t2", "t3", "t4"}
    assert events_written == []   # on_commit never called

    # ---------- Final ledger contains exactly 4 events, no duplicate ids ----------
    with open(repo_path / "ledger" / "events.jsonl") as f:
        lines = f.readlines()
    assert len(lines) == 4
    ledger_ids = {json.loads(line)["id"] for line in lines}
    assert ledger_ids == {"t1", "t2", "t3", "t4"}
