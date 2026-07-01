"""BoBClaw build pipeline P2 — verify/repair loop tests.

The verify gate runs real subprocesses (deterministic, network-free); the apex/worker
backends are mocked. Covers the converge predicate, the gate report, the repair pass
(fixes impls only, never the tests), and two integrated loops: (1) a fixable failure
that repair closes to green, and (2) a self-contradictory contract (bad expected
value) that stays RED through repair and is SURFACED in the final report — never
masked, never auto-fixed by editing the test.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import core.config as cfg
from core.build import skeleton
from core.build.contracts import repair_prompt
from core.graph import (
    _route_after_dispatch,
    _route_after_join,
    _route_after_plan,
    _route_after_recall,
)
from core.nodes.build_verify import (
    _route_after_verify,
    _terminal,
    build_green,
    repair_node,
    verify_node,
)
from langgraph.graph import END

_GREEN = {"builds": True, "runs": True, "passed": 2, "failed": 0, "errors": 0}
_RED = {"builds": True, "runs": True, "passed": 1, "failed": 1, "errors": 0, "failing": ["x"]}


@pytest.fixture(autouse=True)
def _force_subprocess_sandbox(monkeypatch):
    # The verify gate runs on the HOST in these tests (deterministic, no Docker
    # dependency); the dockerized path is covered in test_build_docker.py.
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "subprocess")


# ── pure predicates ──────────────────────────────────────────────────────────

def test_build_green():
    assert build_green(_GREEN)
    assert not build_green({**_GREEN, "failed": 2})
    assert not build_green({**_GREEN, "errors": 1})
    assert not build_green({**_GREEN, "builds": False})
    assert not build_green({**_GREEN, "runs": False})
    # 0 passed / 0 failed / 0 errors is NOT green (no tests actually ran / unparsed output)
    assert not build_green({"builds": True, "runs": True, "passed": 0, "failed": 0, "errors": 0})


def test_parse_pytest_summary_anchors_on_summary_line():
    # A stray "N failed/passed/error"-shaped substring in captured output must NOT
    # corrupt the counts — only the final summary line is read (red stays red).
    from core.build.skeleton import parse_pytest_summary
    out = ("test session starts\n"
           "status: 0 failed, all good\n"            # impl's own logging — must be ignored
           "FAILED tests/test_functions.py::test_add - assert 2 == 3\n"
           "1 failed, 7 passed in 0.12s\n")
    r = parse_pytest_summary(out)
    assert r["passed"] == 7 and r["failed"] == 1 and r["errors"] == 0
    assert r["failing"] == ["add"]


def test_terminal_and_route(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 1)
    assert _terminal(_GREEN, 0) and _terminal(_GREEN, 5)        # green always converges
    assert not _terminal(_RED, 0)                               # red, budget left → loop
    assert _terminal(_RED, 1)                                   # red, budget spent → converge
    assert _route_after_verify({"verify_report": _GREEN, "repair_round": 0}) == END
    assert _route_after_verify({"verify_report": _RED, "repair_round": 0}) == "repair"
    assert _route_after_verify({"verify_report": _RED, "repair_round": 1}) == END


def test_terminal_budget_zero_never_repairs(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 0)
    assert _terminal(_RED, 0)                                   # budget 0 → converge immediately
    assert _route_after_verify({"verify_report": _RED, "repair_round": 0}) == END


def test_merge_impls_repair_supersedes_worker():
    # build_impls is operator.add: worker entry then a same-name repair entry → repair wins.
    entries = [
        {"idx": 0, "name": "f", "source": "def f():\n    return 1", "status": "ok"},
        {"idx": 1, "name": "g", "source": None, "status": "no_impl"},
        {"idx": 0, "name": "f", "source": "def f():\n    return 2", "status": "repaired"},
    ]
    merged = skeleton.merge_impls(entries)
    assert merged == {"f": "def f():\n    return 2"}            # repair won; g (None) dropped


def test_repair_prompt_asks_for_impl_source_only():
    units = [{"name": "addtwo", "signature": "addtwo(a, b)", "doc": "sum",
              "cases": [{"args": [1, 2], "expect": 3}]}]
    p = repair_prompt(units)
    assert "addtwo" in p
    assert "corrected function source" in p                    # fixes impls, not tests
    assert "edit the test" not in p.lower()


# ── verify_node (real subprocess gate) ───────────────────────────────────────

_ADD = {"name": "addtwo", "signature": "addtwo(a, b)", "doc": "sum",
        "cases": [{"args": [1, 2], "expect": 3}]}


def _ws(tmp_path, impls):
    skeleton.write_app(tmp_path, [_ADD], impls)
    return {"build_contracts": [_ADD], "build_workspace": str(tmp_path), "repair_round": 0}


async def test_verify_green_emits_once(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 1)
    state = _ws(tmp_path, {"addtwo": "def addtwo(a, b):\n    return a + b"})
    with patch("core.nodes.build_verify._append_agent_turn_event", AsyncMock()):
        out = await verify_node(state)
    r = out["verify_report"]
    assert r["builds"] and r["runs"] and r["passed"] == 1 and r["failed"] == 0
    assert out["messages"][0]["content"].startswith("Built app: builds=True")
    state.update(out)
    assert _route_after_verify(state) == END


async def test_verify_red_not_terminal_routes_to_repair(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 1)
    state = _ws(tmp_path, {"addtwo": "def addtwo(a, b):\n    return None"})  # wrong impl
    with patch("core.nodes.build_verify._append_agent_turn_event", AsyncMock()):
        out = await verify_node(state)
    assert out["verify_report"]["failed"] == 1
    assert out["verify_report"]["failing"] == ["addtwo"]
    assert "messages" not in out                               # not terminal → no emit
    state.update(out)
    assert _route_after_verify(state) == "repair"


async def test_verify_missing_workspace_fails_loud(monkeypatch):
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 1)
    with patch("core.nodes.build_verify._append_agent_turn_event", AsyncMock()):
        out = await verify_node({"repair_round": 0})
    assert out["error"] and "no workspace" in out["error"]
    assert out["messages"]                                      # surfaced once
    # FATAL → terminal via the single predicate, so the router AGREES → END (no
    # emit-then-repair-then-emit double-emit at the default budget).
    assert out["verify_report"]["fatal"] is True
    state = {"repair_round": 0, **out}
    assert _route_after_verify(state) == END


async def test_verify_timeout_is_fatal_red_not_a_crash(tmp_path, monkeypatch):
    """A hanging impl (run_pytest raises TimeoutExpired) is surfaced as a FATAL red
    gate (terminal, one message, no repair), never an uncaught crash."""
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 1)
    state = _ws(tmp_path, {"addtwo": "def addtwo(a, b):\n    return a + b"})

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=["pytest"], timeout=1)

    monkeypatch.setattr(skeleton, "run_pytest", _boom)
    with patch("core.nodes.build_verify._append_agent_turn_event", AsyncMock()):
        out = await verify_node(state)
    assert out["verify_report"]["fatal"] is True
    assert "timed out" in out["error"]
    assert out["messages"]                                      # surfaced once, no crash
    state.update(out)
    assert _route_after_verify(state) == END                   # fatal → no repair


# ── repair_node: fixes impls, re-writes app, never edits the tests ───────────

async def test_repair_fixes_impl_and_preserves_tests(tmp_path):
    state = _ws(tmp_path, {"addtwo": "def addtwo(a, b):\n    return None"})  # wrong
    state["build_impls"] = [{"idx": 0, "name": "addtwo", "source": None, "status": "no_impl"}]
    state["verify_report"] = {"failing": ["addtwo"]}
    state["team"] = "demo-fleet"

    async def _repair_send(messages, backend):
        assert backend == "claude_api"                         # apex role
        return json.dumps({"addtwo": "def addtwo(a, b):\n    return a + b"})

    with patch("core.nodes.build_verify._send_to_backend", _repair_send):
        out = await repair_node(state)

    assert out["repair_round"] == 1
    assert out["build_impls"][0]["name"] == "addtwo"
    assert "return a + b" in out["build_impls"][0]["source"]
    fns = (tmp_path / "minikit" / "functions.py").read_text(encoding="utf-8")
    assert "return a + b" in fns                               # app re-written with the fix
    suite = (tmp_path / "tests" / "test_functions.py").read_text(encoding="utf-8")
    assert "addtwo(1, 2) == 3" in suite                        # the TEST is untouched


async def test_repair_noop_on_unparseable_apex_still_increments(tmp_path):
    # A repair that fixes NOTHING (apex returns unparseable JSON) must still increment
    # repair_round so the loop terminates by budget, not stall — and return no fixes.
    state = _ws(tmp_path, {"addtwo": "def addtwo(a, b):\n    return None"})
    state["build_impls"] = [{"idx": 0, "name": "addtwo", "source": None, "status": "no_impl"}]
    state["verify_report"] = {"failing": ["addtwo"]}
    state["team"] = "demo-fleet"

    async def _bad(messages, backend):
        return "I could not produce valid JSON, sorry."

    with patch("core.nodes.build_verify._send_to_backend", _bad):
        out = await repair_node(state)
    assert out["repair_round"] == 1
    assert out["build_impls"] == []                            # no fixes from a bad reply


# ── integrated loops (real verify subprocess, mocked apex/worker) ────────────

def _apply(state, delta):
    for k, v in delta.items():
        if k in ("messages", "build_impls", "worker_results"):
            state[k] = (state.get(k) or []) + list(v)
        else:
            state[k] = v


async def _drive_through_join(state, plan_send, worker_send):
    """plan_contracts → dispatch → worker×N → join, then return the verify route."""
    from core.nodes.build_plan import plan_contracts_node
    from core.nodes.dispatch import dispatch_node
    from core.nodes.join import join_node
    from core.nodes.worker import worker_node

    assert _route_after_recall(state) == "plan_contracts"
    with patch("core.nodes.build_plan._send_to_backend", plan_send):
        _apply(state, await plan_contracts_node(state))
    assert _route_after_plan(state) == "dispatch"
    _apply(state, dispatch_node(state))
    sends = _route_after_dispatch(state)
    with patch("core.nodes.worker._send_to_backend", worker_send):
        for s in sends:
            _apply(state, await worker_node(s.arg))
    _apply(state, await join_node(state))
    return _route_after_join(state)


async def _verify_repair_loop(state, repair_send):
    """Run verify → {repair → verify}* → END, returning how many verifies/repairs ran."""
    verifies = repairs = 0
    while True:
        with patch("core.nodes.build_verify._append_agent_turn_event", AsyncMock()):
            _apply(state, await verify_node(state))
        verifies += 1
        if _route_after_verify(state) == END:
            break
        with patch("core.nodes.build_verify._send_to_backend", repair_send):
            _apply(state, await repair_node(state))
        repairs += 1
    return verifies, repairs


async def test_repair_loop_closes_a_fixable_failure(tmp_path, monkeypatch):
    """Worker ships a wrong impl → verify red → repair fixes it → verify green → END,
    with exactly ONE final message (the green one)."""
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 1)

    async def _plan(messages, backend):
        return json.dumps({"units": [_ADD]})              # correct spec (expect 3)

    async def _worker(messages, backend):
        return "def addtwo(a, b):\n    return None"        # WRONG impl

    async def _repair(messages, backend):
        return json.dumps({"addtwo": "def addtwo(a, b):\n    return a + b"})  # the fix

    state = {"task": "build", "build_request": True, "team": "demo-fleet",
             "conversation_id": "c", "build_units": 1}
    assert await _drive_through_join(state, _plan, _worker) == "verify"
    verifies, repairs = await _verify_repair_loop(state, _repair)

    assert (verifies, repairs) == (2, 1)                  # red → repair → green
    assert state["repair_round"] == 1
    r = state["verify_report"]
    assert r["builds"] and r["runs"] and r["passed"] == 1 and r["failed"] == 0
    assistants = [m for m in state["messages"] if m.get("role") == "assistant"]
    assert len(assistants) == 1                           # exactly one final answer
    fns = (Path(state["build_workspace"]) / "minikit" / "functions.py").read_text(encoding="utf-8")
    assert "return a + b" in fns


async def test_repair_loop_runs_multiple_rounds(tmp_path, monkeypatch):
    """budget=2: a no-op first repair, a fixing second repair → 3 verifies / 2 repairs,
    repair_round threaded across both rounds, exactly one final (green) message."""
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 2)

    async def _plan(messages, backend):
        return json.dumps({"units": [_ADD]})

    async def _worker(messages, backend):
        return "def addtwo(a, b):\n    return None"        # wrong

    calls = {"n": 0}

    async def _repair(messages, backend):
        calls["n"] += 1
        if calls["n"] == 1:
            return "no json here"                          # round 1: no-op, stays red
        return json.dumps({"addtwo": "def addtwo(a, b):\n    return a + b"})  # round 2: fix

    state = {"task": "build", "build_request": True, "team": "demo-fleet",
             "conversation_id": "c", "build_units": 1}
    assert await _drive_through_join(state, _plan, _worker) == "verify"
    verifies, repairs = await _verify_repair_loop(state, _repair)

    assert (verifies, repairs) == (3, 2)                   # red → noop → red → fix → green
    assert state["repair_round"] == 2
    assert state["verify_report"]["failed"] == 0
    assistants = [m for m in state["messages"] if m.get("role") == "assistant"]
    assert len(assistants) == 1


async def test_bad_spec_stays_surfaced_never_masked(tmp_path, monkeypatch):
    """A self-contradictory contract (addtwo(1,2) expect 999) with a CORRECT impl stays
    RED through repair and lands in the final report — the gate SURFACES the bad spec,
    never edits the test to make it pass (the demo's hallucinated-sha256 class)."""
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(cfg, "BUILD_REPAIR_BUDGET", 1)
    bad = {"name": "addtwo", "signature": "addtwo(a, b)", "doc": "sum",
           "cases": [{"args": [1, 2], "expect": 999}]}            # WRONG expected value

    async def _plan(messages, backend):
        return json.dumps({"units": [bad]})

    async def _worker(messages, backend):
        return "def addtwo(a, b):\n    return a + b"               # genuinely correct (=3)

    async def _repair(messages, backend):
        return json.dumps({"addtwo": "def addtwo(a, b):\n    return a + b"})  # honest, still 3

    state = {"task": "build", "build_request": True, "team": "demo-fleet",
             "conversation_id": "c", "build_units": 1}
    assert await _drive_through_join(state, _plan, _worker) == "verify"
    verifies, repairs = await _verify_repair_loop(state, _repair)

    assert (verifies, repairs) == (2, 1)                  # tried a repair, still red
    r = state["verify_report"]
    assert r["failed"] == 1 and r["failing"] == ["addtwo"]   # SURFACED, not masked
    assert "Failing: addtwo" in state["messages"][-1]["content"]
    # exactly-once on the RED (budget-spent) terminal path too, not just the green one:
    assert len([m for m in state["messages"] if m.get("role") == "assistant"]) == 1
    # The gate never edited the test to make the bad spec pass:
    suite = (Path(state["build_workspace"]) / "tests" / "test_functions.py").read_text(encoding="utf-8")
    assert "addtwo(1, 2) == 999" in suite
