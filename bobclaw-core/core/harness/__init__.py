"""
BoBClaw Core — brain / hands / session decoupling (§2.2).

Three swappable interfaces over the existing topology, purely additive:
  * **session** — :class:`core.harness.session.LedgerSession` (wraps the locked
    ``ledger_slice`` / ``read_ledger_at`` — the §2.1 session-as-ledger-slice object).
  * **hands** — :class:`core.harness.hands.BackendHands` / :class:`RegistryHands`, the
    uniform ``execute(name, input) -> string`` over the existing backend dispatch.
  * **sandbox** — :class:`core.harness.sandbox.SubprocessSandbox` (the production
    isolation sandbox ``core.build.sandbox`` satisfies the same protocol).

The harness (:mod:`core.harness.supervisor`) is the cattle-retry loop + replay-from-ledger.
"""
from core.harness.interfaces import (
    FatalToolCallError,
    Hands,
    RetryableToolCallError,
    Sandbox,
    Session,
    WorkerError,
)
from core.harness.hands import BackendHands, RegistryHands
from core.harness.sandbox import SandboxResult, SubprocessSandbox
from core.harness.session import LedgerSession
from core.harness.supervisor import (
    TaskSpec,
    TrajectoryResult,
    replay_and_resume,
    run_fanout,
    supervise_task,
)

__all__ = [
    # error model
    "WorkerError",
    "RetryableToolCallError",
    "FatalToolCallError",
    # protocols
    "Hands",
    "Session",
    "Sandbox",
    # impls
    "BackendHands",
    "RegistryHands",
    "LedgerSession",
    "SubprocessSandbox",
    "SandboxResult",
    # harness
    "TaskSpec",
    "TrajectoryResult",
    "supervise_task",
    "run_fanout",
    "replay_and_resume",
]
