"""BoBClaw build pipeline — P2: the verify/repair loop.

``verify_node`` runs the gate (build + pytest + CLI) on the sandbox app and records
the honest result on ``verify_report``. ``_route_after_verify`` converges (END) when
the build is GREEN (builds + runs + 0 failed/errored tests) OR the repair budget is
spent, else routes to ``repair_node`` — a bounded apex pass that fixes ONLY the
failing IMPLEMENTATIONS (never the tests) and re-writes the app, then loops back to
verify. The loop is bounded by ``BUILD_REPAIR_BUDGET`` (the ``repair_round`` counter),
mirroring the fan-out wave loop.

The gate SURFACES a bad spec, never masks it: the tests are generated from the
contracts' declared cases and repair is never given the tests, so a self-contradictory
contract (a hallucinated expected value) stays red through every repair pass and lands
in the final, honest ``verify_report`` (the demo's 1 red sha256 case is the canonical
example).

``verify_node`` is the SOLE emitter of the build turn's final answer: it appends the
assistant message + L0 event ONLY on the terminal (converge) pass, so the turn
produces exactly one answer regardless of how many repair rounds ran (``join`` emits
nothing for a build; this mirrors the council close-gate's exactly-once commit).
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

import core.config as _config
import core.teams as teams
from core.build import contracts, sandbox, skeleton
from core.nodes._l0_events import _append_agent_turn_event
from core.nodes.execute import _send_to_backend
from langgraph.graph import END

logger = logging.getLogger(__name__)


def build_green(report: dict) -> bool:
    """True iff the gate is fully green: builds + runs + at least one PASSING test +
    no failed/errored tests.

    The ``passed > 0`` clause is load-bearing: a run that parses to 0 passed / 0 failed
    / 0 errors (pytest 'no tests ran', a container abend, or output the summary parser
    can't match) must NOT count as green — every contract now carries a case, so a real
    green build always has passed >= 1.
    """
    return (
        bool(report.get("builds")) and bool(report.get("runs"))
        and report.get("passed", 0) > 0
        and not report.get("failed") and not report.get("errors")
    )


def _terminal(report: dict, repair_round: int) -> bool:
    """The SINGLE converge predicate (single-sourced like the council's defer rule):
    converge when the build is FATAL (unrepairable: no workspace / verify timed out),
    GREEN, or the repair budget is spent. Read by BOTH ``verify_node`` (emit the final
    answer iff terminal) and ``_route_after_verify`` (END iff terminal) so the
    sole-emitter contract can never disagree with the router (the streaming-drop /
    double-emit class). A ``fatal`` report routes through the SAME predicate — never
    emit-unconditionally-and-hope, which is exactly the disagreement vector."""
    return (bool(report.get("fatal")) or build_green(report)
            or repair_round >= _config.BUILD_REPAIR_BUDGET)


def _summary(report: dict) -> str:
    """Honest one-line build result — surfaces failing test names, never hides them."""
    bits = [
        f"Built app: builds={report.get('builds')} runs={report.get('runs')}",
        f"tests {report.get('passed', 0)} passed / {report.get('failed', 0)} failed",
    ]
    if report.get("errors"):
        bits.append(f"{report['errors']} errors")
    line = ", ".join(bits)
    if report.get("failing"):
        line += f". Failing: {', '.join(report['failing'])}"
    if report.get("workspace"):
        line += f" (workspace: {report['workspace']})"
    return line + "."


async def verify_node(state: dict) -> dict:
    """Run the build gate on the sandbox; emit the final answer iff converging.

    Returns ``verify_report`` (builds/passed/failed/errors/runs/failing). On the
    terminal pass (green or budget spent) it ALSO appends the single assistant
    message + L0 event (sole emitter). An intermediate (will-repair) pass emits
    nothing, so the turn surfaces exactly one answer.
    """
    workspace = state.get("build_workspace")
    repair_round = state.get("repair_round", 0)
    gate_error: str | None = None

    if not workspace:
        # Defensive: plan/join always set a workspace. A missing one can't be
        # repaired → FATAL (terminal via _terminal, surfaced exactly once below — NOT
        # emitted unconditionally, which would disagree with the router).
        gate_error = "build verify: no workspace to verify."
        report = {"phase": "verify", "builds": False, "passed": 0, "failed": 0,
                  "errors": 0, "runs": False, "failing": [], "workspace": None,
                  "repair_round": repair_round, "fatal": True}
    else:
        ws = Path(workspace)
        try:
            # The gate EXECUTES the LLM-written impls → run it through the sandbox
            # dispatcher (Docker isolation when available; host per BUILD_SANDBOX).
            # Resolve the mode ONCE and thread it so build / test / CLI provably use the
            # SAME mode (no mid-pass split) and the daemon is probed once, not per call.
            # These shell out (subprocess.run / docker run) and would block the shared
            # event loop for up to BUILD_VERIFY_TIMEOUT — offload to a worker thread
            # (the codebase's async-subprocess convention, cf. backends/claude_code.py).
            mode = await asyncio.to_thread(sandbox.resolve_mode)
            builds = await asyncio.to_thread(sandbox.build_empty_ok, ws, mode=mode)
            gate = await asyncio.to_thread(
                sandbox.run_pytest, ws, timeout=_config.BUILD_VERIFY_TIMEOUT, mode=mode)
            runs, _cli_out = await asyncio.to_thread(sandbox.run_cli, ws, mode=mode)
            report = {
                "phase": "verify", "builds": builds, "passed": gate["passed"],
                "failed": gate["failed"], "errors": gate["errors"], "runs": runs,
                "failing": gate["failing"], "workspace": workspace,
                "repair_round": repair_round,
            }
            logger.info(
                "build verify (round %d): builds=%s passed=%d failed=%d errors=%d runs=%s",
                repair_round, builds, gate["passed"], gate["failed"], gate["errors"], runs,
            )
        except sandbox.SandboxUnavailable as exc:
            # BUILD_SANDBOX=docker forced but unavailable — FAIL LOUD, never fall back to
            # un-isolated host execution of LLM code.
            gate_error = f"build verify: {exc}"
            logger.error("build verify (round %d): %s", repair_round, gate_error)
            report = {"phase": "verify", "builds": False, "passed": 0, "failed": 0,
                      "errors": 0, "runs": False, "failing": [], "workspace": workspace,
                      "repair_round": repair_round, "fatal": True}
        except subprocess.TimeoutExpired as exc:
            # A hanging impl/CLI: surface a FATAL red gate (fail-loud, never mask as
            # green, never crash the turn). A suite-level hang can't be attributed to a
            # unit, so terminate rather than burn repair rounds on an untargetable fix.
            gate_error = f"build verify timed out after {exc.timeout}s."
            logger.warning("build verify (round %d) timed out: %s", repair_round, gate_error)
            report = {"phase": "verify", "builds": False, "passed": 0, "failed": 0,
                      "errors": 0, "runs": False, "failing": [], "workspace": workspace,
                      "repair_round": repair_round, "fatal": True}

    out: dict = {"verify_report": report}
    if _terminal(report, repair_round):
        body = gate_error or _summary(report)
        await _append_agent_turn_event(state, assistant_response=body, error_msg=gate_error)
        out["messages"] = [{"role": "assistant", "content": body}]
        if gate_error:
            out["error"] = gate_error
    return out


def _route_after_verify(state: dict) -> str:
    """Converge to END when terminal (green or budget spent), else loop to repair.

    Uses the SAME ``_terminal`` predicate ``verify_node`` used, on the report
    ``verify_node`` just set (``repair_round`` unchanged between them), so the router
    and the sole-emitter never disagree.
    """
    report = state.get("verify_report") or {}
    if _terminal(report, state.get("repair_round", 0)):
        return END
    return "repair"


async def _repair_failing(failing, units_by_name, backend, cap) -> dict[str, str]:
    """Bounded apex pass over failing units → ``{name: clean def source}``.

    Fixes ONLY implementations — repair is never given the tests, and the contracts/
    cases are unchanged, so a bad spec cannot be auto-fixed away. Tolerant parse:
    unparseable apex output → no fixes (the units stay failing and get surfaced).
    """
    todo = [units_by_name[n] for n in failing if n in units_by_name][:cap]
    if not todo:
        return {}
    out = await _send_to_backend(
        [{"role": "user", "content": contracts.repair_prompt(todo)}], backend)
    try:
        data = json.loads(contracts.unfence(out))
    except json.JSONDecodeError:
        return {}
    fixed: dict[str, str] = {}
    for name, src in (data.items() if isinstance(data, dict) else []):
        clean = contracts.extract_func(str(src), name)
        if not clean:
            continue
        # P3 sandbox gate — MIRROR the worker path: a repair pass is LLM-written code on
        # the same write+execute path, so it must pass the same static safety gate. An
        # unsafe fix is DROPPED (the unit stays failing → surfaced), never written/run.
        safe, reason = contracts.is_safe_impl(clean)
        if not safe:
            logger.warning("build repair: dropped unsafe fix for %r (%s)", name, reason)
            continue
        fixed[name] = clean
    return fixed


async def repair_node(state: dict) -> dict:
    """One bounded apex repair pass over the failing units → re-write the app.

    Fixes ONLY implementations (the contracts/tests are untouched), merges the fixes
    over the current impls, re-writes the sandbox app, and increments ``repair_round``.
    Returns the fixes as ``build_impls`` entries (``operator.add`` → they append after
    the worker entries, so ``merge_impls``' last-wins lets the fix supersede the
    original impl on the next verify and in the final state).
    """
    contracts_list = state.get("build_contracts") or []
    workspace = state.get("build_workspace")
    report = state.get("verify_report") or {}
    failing = report.get("failing") or []
    repair_round = state.get("repair_round", 0)

    units_by_name = {u["name"]: u for u in contracts_list}
    pos = {u["name"]: i for i, u in enumerate(contracts_list)}
    apex = teams.role_backend(state.get("team"), "apex") or state.get("backend") or "local"

    fixed = await _repair_failing(
        failing, units_by_name, apex, _config.BUILD_REPAIR_UNIT_CAP)

    current = skeleton.merge_impls(state.get("build_impls") or [])
    merged = {**current, **fixed}
    if workspace:
        skeleton.write_app(Path(workspace), contracts_list, merged)

    logger.info("build repair (round %d → %d): %d failing, %d fixed",
                repair_round, repair_round + 1, len(failing), len(fixed))
    entries = [
        {"idx": pos.get(name, 0), "name": name, "source": src, "status": "repaired"}
        for name, src in fixed.items()
    ]
    return {"build_impls": entries, "repair_round": repair_round + 1}
