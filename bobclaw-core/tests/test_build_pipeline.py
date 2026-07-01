"""BoBClaw build pipeline P1 — fan-out wiring tests (network-free).

Covers the build branch of the reused fan-out (dispatch → worker → join), the entry
trigger + routing (recall → plan_contracts → dispatch → ... → END), team-based
apex/worker backend resolution, and an integrated in-graph sub-path run (real
routing helpers + nodes, mocked backends) that builds a tiny app end to end.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import core.config as cfg
import core.teams as teams
from core.graph import (
    _route_after_join,
    _route_after_plan,
    _route_after_recall,
    build_graph,
)
from langgraph.graph import END
from langgraph.types import Send

_C1 = {"name": "addtwo", "signature": "addtwo(a, b)", "doc": "sum",
       "cases": [{"args": [1, 2], "expect": 3}]}
_C2 = {"name": "neg", "signature": "neg(x)", "doc": "negate",
       "cases": [{"args": [5], "expect": -5}]}


@pytest.fixture(autouse=True)
def _force_subprocess_sandbox(monkeypatch):
    # In-graph e2e runs the verify gate on the HOST (deterministic, no Docker dep).
    monkeypatch.setattr(cfg, "BUILD_SANDBOX", "subprocess")


# ── entry trigger + routing ──────────────────────────────────────────────────

def test_recall_build_arm_takes_precedence_and_is_gated():
    # build_request → plan_contracts, even alongside a council_spec (own arm).
    assert _route_after_recall({"build_request": True}) == "plan_contracts"
    assert _route_after_recall({"build_request": True, "council_spec": {}}) == "plan_contracts"
    # Absent/falsy trigger → byte-identical to today.
    assert _route_after_recall({}) == "dispatch"
    assert _route_after_recall({"build_request": False}) == "dispatch"
    assert _route_after_recall({"council_spec": {"mode": "fusion"}}) == "panel_dispatch"


def test_route_after_plan_fails_loud_or_proceeds():
    assert _route_after_plan({"error": "boom"}) == END               # fail-loud
    assert _route_after_plan({}) == END                              # no contracts
    assert _route_after_plan({"build_contracts": []}) == END         # empty
    assert _route_after_plan({"build_contracts": [_C1]}) == "dispatch"
    # An unbuildable-skeleton abort sets BOTH contracts and error → still END.
    assert _route_after_plan({"build_contracts": [_C1], "error": "x"}) == END


def test_route_after_join_build_branch_routes_to_verify():
    # P2: the build join hands off to the verify gate (was END in P1).
    assert _route_after_join({"build_contracts": [_C1]}) == "verify"


def test_graph_wires_plan_contracts():
    nodes = set(build_graph().get_graph().nodes.keys())
    assert "plan_contracts" in nodes


# ── team-based backend resolution ────────────────────────────────────────────

def test_role_backend_resolves_demo_fleet_split():
    assert teams.role_backend("demo-fleet", "apex") == "claude_api"
    assert teams.role_backend("demo-fleet", "worker") == "deepseek_v4_flash"
    assert teams.role_backend(None, "worker") is None      # no team → caller fallback


def test_build_worker_backend_falls_back_to_state_backend():
    from core.nodes.dispatch import _build_worker_backend
    assert _build_worker_backend({"team": "demo-fleet"}) == "deepseek_v4_flash"
    assert _build_worker_backend({"backend": "glm_5_2"}) == "glm_5_2"
    assert _build_worker_backend({}) == "local"


# ── build dispatch: Sends per contract + fail-loud pre-flight ────────────────

def test_build_dispatch_emits_one_send_per_contract():
    from core.nodes.dispatch import _route_after_dispatch, dispatch_node
    state = {"build_contracts": [_C1, _C2], "team": "demo-fleet",
             "build_workspace": "/ws", "escalation_backend": None}
    assert dispatch_node(state) == {}                      # cost ok, no error
    sends = _route_after_dispatch(state)
    assert isinstance(sends, list) and len(sends) == 2
    assert all(isinstance(s, Send) for s in sends)
    args = [s.arg for s in sends]
    assert [a["build_contract"]["name"] for a in args] == ["addtwo", "neg"]
    assert [a["subtask_idx"] for a in args] == [0, 1]
    assert all(a["backend"] == "deepseek_v4_flash" for a in args)   # worker role
    assert all(a["build_workspace"] == "/ws" for a in args)


def test_build_dispatch_single_contract_still_fans_out():
    # The build branch returns Sends UNCONDITIONALLY (before the chat path's
    # `len > 1 → execute` guard), so a 1-contract build still fans out, never
    # falling to the chat execute path — the build/chat divergence point.
    from core.nodes.dispatch import _route_after_dispatch
    sends = _route_after_dispatch({"build_contracts": [_C1], "team": "demo-fleet",
                                   "build_workspace": "/ws", "escalation_backend": None})
    assert isinstance(sends, list) and len(sends) == 1
    assert isinstance(sends[0], Send)
    assert sends[0].arg["backend"] == "deepseek_v4_flash"


def test_build_dispatch_aborts_over_global_cap():
    from core.nodes.dispatch import dispatch_node
    contracts = [{"name": f"f{i}", "signature": f"f{i}()", "doc": "", "cases": []}
                 for i in range(101)]
    out = dispatch_node({"build_contracts": contracts, "team": "demo-fleet"})
    assert "error" in out and "100" in out["error"]


def test_build_dispatch_aborts_over_hard_account_cap():
    # A hard per-account-capped worker (kimi_code, cap 10) + >cap contracts fails
    # LOUD instead of firing N concurrent Sends that silently 429-degrade.
    from core.nodes.dispatch import dispatch_node
    contracts = [{"name": f"f{i}", "signature": f"f{i}()", "doc": "", "cases": []}
                 for i in range(11)]
    out = dispatch_node({"build_contracts": contracts, "backend": "kimi_code"})
    assert "error" in out and "kimi_code" in out["error"] and "10" in out["error"]


def test_build_dispatch_allows_spawn_unbounded_above_per_backend_cap():
    # deepseek (per-backend cap 20, but demo-proved 100 concurrent) is spawn-unbounded:
    # a 25-contract build passes pre-flight (bounded only by the global cap), NOT
    # blocked by the per-backend cap — that distinction is the point of the hard-cap set.
    from core.nodes.dispatch import dispatch_node
    contracts = [{"name": f"f{i}", "signature": f"f{i}()", "doc": "", "cases": []}
                 for i in range(25)]
    assert dispatch_node({"build_contracts": contracts, "team": "demo-fleet"}) == {}


def test_build_dispatch_aborts_on_unmapped_worker_backend():
    from core.nodes.dispatch import dispatch_node
    out = dispatch_node({"build_contracts": [_C1], "backend": "totally_unknown"})
    assert "error" in out and "MAX_WORKER_USD_BY_BACKEND" in out["error"]


def test_build_dispatch_cost_preflight_fails_loud(monkeypatch):
    from core.nodes import dispatch
    monkeypatch.setattr(dispatch, "remaining_budget", lambda b: 0.0)
    out = dispatch.dispatch_node({"build_contracts": [_C1, _C2], "team": "demo-fleet"})
    assert "error" in out and "cost-cap" in out["error"]


# ── build worker: impl extraction + fail-soft ────────────────────────────────

async def test_build_worker_extracts_impl():
    from core.nodes.worker import worker_node

    async def _send(messages, backend):
        m = re.search(r"Signature: def (\w+)\(([^)]*)\):", messages[-1]["content"])
        return f"def {m.group(1)}({m.group(2)}):\n    return None"

    with patch("core.nodes.worker._send_to_backend", _send):
        out = await worker_node({"build_contract": _C1, "subtask_idx": 0,
                                 "backend": "deepseek_v4_flash"})
    entry = out["build_impls"][0]
    assert entry["name"] == "addtwo" and entry["status"] == "ok"
    assert "def addtwo" in entry["source"]


async def test_build_worker_no_impl_on_garbage():
    from core.nodes.worker import worker_node

    async def _send(messages, backend):
        return "Sorry, I cannot write that function. Here is some prose."

    with patch("core.nodes.worker._send_to_backend", _send):
        out = await worker_node({"build_contract": _C1, "subtask_idx": 0,
                                 "backend": "deepseek_v4_flash"})
    entry = out["build_impls"][0]
    assert entry["source"] is None and entry["status"] == "no_impl"


async def test_build_worker_failsoft_on_exception():
    from core.nodes.worker import worker_node

    async def _send(messages, backend):
        raise RuntimeError("backend exploded")

    with patch("core.nodes.worker._send_to_backend", _send):
        out = await worker_node({"build_contract": _C1, "subtask_idx": 0,
                                 "backend": "deepseek_v4_flash"})
    entry = out["build_impls"][0]
    assert entry["source"] is None and entry["status"] == "failed"


# ── build join: merge by name + re-write the app ─────────────────────────────

async def test_build_join_merges_by_name_and_rewrites(tmp_path):
    # P2: join merges + re-writes the app and sets an interim verify_report, but emits
    # NO message (verify is the sole emitter); routing goes join → verify.
    from core.nodes.join import join_node
    state = {
        "build_contracts": [_C1, _C2],
        "build_workspace": str(tmp_path),
        "build_impls": [
            {"idx": 0, "name": "addtwo", "source": "def addtwo(a, b):\n    return a + b",
             "status": "ok"},
            {"idx": 1, "name": "neg", "source": None, "status": "no_impl"},
        ],
    }
    out = await join_node(state)
    assert out["verify_report"] == {"phase": "built", "units": 2, "implemented": 1,
                                    "workspace": str(tmp_path)}
    assert "messages" not in out                       # join no longer emits (verify does)
    fns = (tmp_path / "minikit" / "functions.py").read_text(encoding="utf-8")
    assert "return a + b" in fns                       # addtwo impl landed
    assert "raise NotImplementedError" in fns          # neg kept its stub


# ── integrated sub-path: recall → plan → dispatch → worker×N → join → END ─────

async def test_build_pipeline_end_to_end_subpath(tmp_path, monkeypatch):
    """Drive the build sub-path with the REAL routing helpers + nodes (mocked
    backends), as the debate loop test does — verifies the wiring + the app gets
    built, without the brittle decompose/route/recall prologue."""
    monkeypatch.setattr(cfg, "BUILD_WORKSPACE_ROOT", str(tmp_path))
    from core.nodes.build_plan import plan_contracts_node
    from core.nodes.build_verify import _route_after_verify, verify_node
    from core.nodes.dispatch import _route_after_dispatch, dispatch_node
    from core.nodes.join import join_node
    from core.nodes.worker import worker_node

    state = {
        "task": "build a tiny toolkit",
        "build_request": True,
        "team": "demo-fleet",
        "conversation_id": "conv-e2e",
        "build_units": 2,
    }

    def _apply(delta):
        for k, v in delta.items():
            if k in ("messages", "build_impls", "worker_results"):
                state[k] = (state.get(k) or []) + list(v)
            else:
                state[k] = v

    seen = {"apex": [], "worker": []}

    async def _apex(messages, backend):
        seen["apex"].append(backend)
        return json.dumps({"units": [_C1, _C2]})

    # Correct impls → the gate goes green at round 0 (no repair needed).
    _correct = {"addtwo": "def addtwo(a, b):\n    return a + b",
                "neg": "def neg(x):\n    return -x"}

    async def _worker(messages, backend):
        seen["worker"].append(backend)
        m = re.search(r"Signature: def (\w+)\(", messages[-1]["content"])
        return _correct[m.group(1)]

    assert _route_after_recall(state) == "plan_contracts"
    with patch("core.nodes.build_plan._send_to_backend", _apex):
        _apply(await plan_contracts_node(state))
    assert _route_after_plan(state) == "dispatch"
    assert state["build_contracts"] and state["build_workspace"]

    _apply(dispatch_node(state))
    sends = _route_after_dispatch(state)
    assert len(sends) == 2
    with patch("core.nodes.worker._send_to_backend", _worker):
        for s in sends:
            _apply(await worker_node(s.arg))
    assert len(state["build_impls"]) == 2

    _apply(await join_node(state))
    assert _route_after_join(state) == "verify"

    # verify gate (real subprocess) on the written app → green → terminal → END.
    with patch("core.nodes.build_verify._append_agent_turn_event", AsyncMock()):
        _apply(await verify_node(state))
    assert _route_after_verify(state) == END

    report = state["verify_report"]
    assert report["builds"] and report["runs"]
    assert report["passed"] == 2 and report["failed"] == 0 and report["errors"] == 0
    assert state["messages"][-1]["content"].startswith("Built app: builds=True")
    fns = (Path(state["build_workspace"]) / "minikit" / "functions.py").read_text(encoding="utf-8")
    assert "return a + b" in fns and "return -x" in fns
    # HARD REQ #2 — apex ≠ worker held through the real nodes (Opus plans, DeepSeek builds):
    assert seen["apex"] == ["claude_api"]
    assert seen["worker"] == ["deepseek_v4_flash", "deepseek_v4_flash"]
