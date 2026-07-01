"""MS-6 — manager-authored purity + protocol-conformance proof (§2.2).

Two invariants the cattle/replay model depends on:
  * NO module-global MUTABLE state anywhere in ``core.harness`` (multi-process safety — a
    rebooted supervisor must rebuild its state from the LEDGER, never a process-local cache).
  * The default impls satisfy the three swappable ``Protocol``s (so each interface is
    genuinely replaceable).
"""
from __future__ import annotations

import importlib

import pytest

import core.harness as harness
from core.harness.hands import BackendHands, RegistryHands
from core.harness.interfaces import (
    FatalToolCallError,
    Hands,
    RetryableToolCallError,
    Sandbox,
    Session,
    WorkerError,
)
from core.harness.sandbox import SubprocessSandbox
from core.harness.session import LedgerSession
from core.harness.supervisor import TaskSpec, run_fanout

_SUBMODULES = [
    "core.harness.interfaces",
    "core.harness.session",
    "core.harness.hands",
    "core.harness.sandbox",
    "core.harness.supervisor",
]

# Names that are legitimately module-level and immutable-by-contract (constants, the public
# __all__ export list, type aliases) — not mutable runtime state.
_ALLOWED_CONTAINERS = {"__all__", "_ALLOWED_CONTAINERS", "_SUBMODULES"}


def test_no_module_global_mutable_state():
    """No module-level mutable dict/list/set (besides the immutable export __all__).

    Mirrors the MS-4 budget-runtime invariant: any process-local mutable state the
    supervisor relied on for 'what's already done' would break the multi-process
    cattle/replay model (the durable record is the git ledger).
    """
    offenders = []
    for modname in _SUBMODULES:
        mod = importlib.import_module(modname)
        for attr, value in vars(mod).items():
            if attr.startswith("__") and attr.endswith("__"):
                continue
            if attr in _ALLOWED_CONTAINERS:
                continue
            # A class/function/module is fine; a bare module-level dict/list/set is the smell.
            if isinstance(value, (dict, list, set)):
                offenders.append(f"{modname}.{attr} :: {type(value).__name__}")
    assert offenders == [], f"module-global mutable state found: {offenders}"


def test_protocol_conformance():
    """The default impls satisfy the three swappable Protocols (runtime_checkable)."""
    assert isinstance(BackendHands(), Hands)
    assert isinstance(RegistryHands({}), Hands)
    assert isinstance(LedgerSession("/tmp/nope"), Session)
    assert isinstance(SubprocessSandbox(), Sandbox)


def test_error_model_retryable_flags():
    """The cattle error model: RetryableToolCallError is retryable, Fatal is not."""
    assert RetryableToolCallError("x").retryable is True
    assert FatalToolCallError("x").retryable is False
    assert WorkerError("x").retryable is False
    # Both are WorkerError subclasses (a single except WorkerError in the supervisor catches both).
    assert isinstance(RetryableToolCallError("x"), WorkerError)
    assert isinstance(FatalToolCallError("x"), WorkerError)


def test_public_surface_exported():
    """The package re-exports the documented public surface."""
    for name in (
        "WorkerError", "RetryableToolCallError", "FatalToolCallError",
        "Hands", "Session", "Sandbox",
        "BackendHands", "RegistryHands", "LedgerSession", "SubprocessSandbox",
        "SandboxResult", "TaskSpec", "TrajectoryResult",
        "supervise_task", "run_fanout", "replay_and_resume",
    ):
        assert hasattr(harness, name), f"core.harness missing public export: {name}"


async def test_run_fanout_is_pure_repeatable():
    """Two run_fanout calls with identical args + a stateless hands return equal results
    (no hidden cross-call state)."""
    async def hand(inp: str) -> str:
        return f"r-{inp}"

    hands = RegistryHands({"h": hand})
    tasks = [TaskSpec("a", "h", "1"), TaskSpec("b", "h", "2")]

    out1 = await run_fanout(tasks, hands)
    out2 = await run_fanout(tasks, hands)
    assert out1["committed_ids"] == out2["committed_ids"] == {"a", "b"}
    assert out1["ran_ids"] == out2["ran_ids"] == {"a", "b"}
    assert [r.status for r in out1["results"]] == [r.status for r in out2["results"]] == ["ok", "ok"]
