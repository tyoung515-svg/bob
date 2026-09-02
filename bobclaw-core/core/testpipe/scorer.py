"""Test-pipe — external scorer bridge (SPEC §7.2, §9 unit 7).

The contamination-proof half of the fence (SPEC §7.2): the external scorer HOLDS the
hidden tests and executes them in ISOLATION — they NEVER enter a model prompt (that is
guaranteed by construction here, because the scorer writes them straight to a sandbox
workspace and runs pytest; no send seam is involved). This makes hidden-test scoring
contamination-proof by construction.

:class:`SubprocessScorer` reuses ``core.build.sandbox`` (the same isolated pytest the
build verify gate uses). :class:`CallableScorer` lets a unit test inject a MOCK
execution (SPEC MS5-P1 scope: "mock the execution; do NOT run a real dataset"). The
LIVE EvalPlus ``evaluate_functional_correctness`` bridge is the deferred live sweep.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional, Protocol, runtime_checkable

from core.build import sandbox
from core.testpipe.spine import _HEADER
from core.testpipe.types import TestItem


@dataclass
class ScoreResult:
    hidden_pass: bool
    passed: int = 0
    failed: int = 0
    errors: int = 0
    raw: str = ""


@runtime_checkable
class Scorer(Protocol):
    """Scorer contract (SPEC §7.2): given an item + its impl, run the HIDDEN tests in
    isolation and return the contamination-proof verdict."""

    async def score(self, item: TestItem, impl: Optional[str]) -> ScoreResult:
        ...


def write_hidden_suite(workspace: Path, item: TestItem, impl: Optional[str]) -> None:
    """Write ``solution.py`` (impl) + ``tests/test_hidden.py`` (the HELD suite).

    This is the ONLY place hidden tests are materialised, and it is a filesystem
    write into an isolated sandbox — NOT a model prompt (SPEC §7.2)."""
    ws = Path(workspace)
    (ws / "tests").mkdir(parents=True, exist_ok=True)
    sig = item.signature or f"{item.entry_point}()"
    body = impl if impl else f"def {sig}:\n    raise NotImplementedError\n"
    (ws / "solution.py").write_text(_HEADER + body + "\n", encoding="utf-8")
    lines = ["from solution import *  # noqa\n\n"]
    for i, t in enumerate(item.hidden_tests):
        lines.append(f"def test_hidden_{i}():\n    {t}\n\n")
    (ws / "tests" / "test_hidden.py").write_text("".join(lines), encoding="utf-8")


class SubprocessScorer:
    """Run the HIDDEN suite in the sandbox and pass iff all hidden tests pass.

    ``mode='subprocess'`` (default) keeps the scaffold Docker-free; ``docker`` routes
    through the isolated container (SPEC §7.2). Network-free — it executes only the
    item's own tests against the impl."""

    def __init__(self, workspace_root: Path, *, mode: str = "subprocess"):
        self.workspace_root = Path(workspace_root)
        self.mode = mode

    async def score(self, item: TestItem, impl: Optional[str]) -> ScoreResult:
        if not impl:
            return ScoreResult(hidden_pass=False, raw="no impl")
        ws = self.workspace_root / f"score-{item.id}"
        ws.mkdir(parents=True, exist_ok=True)
        write_hidden_suite(ws, item, impl)
        report = sandbox.run_pytest(ws, mode=self.mode)
        hidden_pass = (
            report.get("passed", 0) > 0
            and not report.get("failed") and not report.get("errors")
        )
        return ScoreResult(
            hidden_pass=hidden_pass, passed=report.get("passed", 0),
            failed=report.get("failed", 0), errors=report.get("errors", 0),
            raw=report.get("raw", ""),
        )


class CallableScorer:
    """Wrap an injected execution fn for unit tests (SPEC MS5-P1: mock the execution).

    ``fn(item, impl) -> bool`` decides hidden-pass; the interface is identical to the
    real :class:`SubprocessScorer` so the pipeline binds to either transparently."""

    def __init__(self, fn: Callable[[TestItem, Optional[str]], "bool | Awaitable[bool]"]):
        self.fn = fn

    async def score(self, item: TestItem, impl: Optional[str]) -> ScoreResult:
        import inspect

        out = self.fn(item, impl)
        if inspect.isawaitable(out):
            out = await out
        return ScoreResult(hidden_pass=bool(out))
