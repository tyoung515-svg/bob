from __future__ import annotations

"""
BoBClaw Core — Brain / hands / session decoupling: the swappable interfaces (§2.2).

Virtualize each agent into three swappable interfaces — **session** (the durable
ledger), **harness** (the supervised loop), **sandbox** (the execution env) — each
failing and replacing independently. Workers are *cattle*: a dead worker surfaces as a
RETRYABLE tool-call error the supervisor retries; it never propagates as an unhandled
crash. Hands are exposed uniformly as ``execute(name, input) -> string``.

This module is the PURE contract surface: the typed cattle-error model + the three
``Protocol``s. stdlib + typing only — no I/O, no state (multi-process safe).
"""

from typing import Awaitable, Optional, Protocol, runtime_checkable

from core.harness.sandbox import SandboxResult


# ── The cattle error model (§2.2 "a dead worker surfaces as a tool-call error") ──

class WorkerError(Exception):
    """Base for a hand / worker execution failure.

    Carries the hand ``name`` + ``input`` that failed and a ``retryable`` flag — the
    supervisor's whole cattle decision keys on ``retryable``.
    """

    retryable: bool = False

    def __init__(
        self,
        message: str = "",
        *,
        name: Optional[str] = None,
        input: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.name = name
        self.input = input


class RetryableToolCallError(WorkerError):
    """A DEAD worker surfaces as THIS — a retryable tool-call error the supervisor
    re-dispatches to a fresh worker (§2.2). ``retryable = True``."""

    retryable = True


class FatalToolCallError(WorkerError):
    """A permanent, NON-retryable hand failure (bad input / unknown hand). The
    supervisor stops immediately rather than burning retries. ``retryable = False``."""

    retryable = False


# ── The three swappable interfaces (§2.2) ────────────────────────────────────────

@runtime_checkable
class Hands(Protocol):
    """The uniform hands interface — ``execute(name, input) -> string`` (§2.2)."""

    async def execute(self, name: str, input: str) -> str:  # noqa: A002 — spec spelling
        ...


@runtime_checkable
class Session(Protocol):
    """The durable-ledger context object (§2.1) — context reconstructed by SLICING the
    ledger, never trusting in-window / in-memory history."""

    def slice(self, commit_range: str) -> dict:
        ...

    def truth_at(self, ref: str = "HEAD") -> dict:
        ...

    def committed_ids(self, commit_range: str) -> set:
        ...


@runtime_checkable
class Sandbox(Protocol):
    """The execution-env interface — run a command, reporting a kill/timeout in the
    result rather than raising (so the caller's hands maps a dead process to a
    ``RetryableToolCallError``)."""

    def run(
        self,
        command: list,
        *,
        timeout: Optional[float] = None,
        cwd: Optional[str] = None,
    ) -> SandboxResult:
        ...


# Re-exported for callers that build a RegistryHands mapping.
HandCallable = "Callable[[str], Awaitable[str]]"

__all__ = [
    "WorkerError",
    "RetryableToolCallError",
    "FatalToolCallError",
    "Hands",
    "Session",
    "Sandbox",
    "SandboxResult",
    "Awaitable",
]
