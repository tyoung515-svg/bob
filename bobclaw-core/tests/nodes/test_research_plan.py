"""MS2-R2 — research orchestrator (decompose + deterministic effort-scale).

Pure, network-free unit tests for ``core/nodes/research_plan.py`` + the graph
wiring (``_route_after_research_plan`` + the ``research_request`` arm of
``_route_after_recall``). Proves: the deterministic count→tier map (1 / 2-4 /
10+), the node's decompose-reuse + fanout_width discipline, the fail-loud edge,
and the guard-at-top byte-identical property (a non-research turn is unchanged).
"""
from unittest.mock import AsyncMock

import pytest

import core.nodes.decompose as decompose
from core.graph import (
    _route_after_recall,
    _route_after_research_plan,
    build_graph,
)
from core.nodes.research_plan import (
    TIER_FANOUT,
    TIER_HIER,
    TIER_SINGLE,
    research_plan_node,
    select_research_tier,
)
from langgraph.graph import END


# ── 1. select_research_tier boundaries (all three spec tiers + the 5-9 band) ──
def test_tier_map_boundaries():
    assert select_research_tier(0) == TIER_SINGLE
    assert select_research_tier(1) == TIER_SINGLE
    for n in range(2, 10):  # the FULL 2..9 fan-out band (incl. 6,7,8 — no special-casing)
        assert select_research_tier(n) == TIER_FANOUT
    for n in (10, 11, 40):
        assert select_research_tier(n) == TIER_HIER


# ── 2. node decomposes when subtasks absent ──────────────────────────────────
@pytest.mark.asyncio
async def test_node_decomposes_when_absent(monkeypatch):
    mock_llm = AsyncMock(return_value=["a", "b", "c"])
    monkeypatch.setattr(decompose, "_call_llm", mock_llm)

    state = {"task": "compare a vs b vs c", "backend": "deepseek_v4_flash"}
    out = await research_plan_node(state)

    assert out["subtasks"] == ["a", "b", "c"]
    assert out["research_tier"] == TIER_FANOUT
    assert out["fanout_width"] == 3
    msgs = out["messages"]
    assert len(msgs) >= 1
    assert any(m.get("role") == "system" for m in msgs)
    mock_llm.assert_awaited_once()


# ── 3. node ALWAYS decomposes on the RESOLVED research backend ────────────────
# (NOT the stale upstream `decompose_node` subtasks: that node runs pre-route at
# backend="local" and fail-opens to [task], which would wrongly force single-tier.)
@pytest.mark.asyncio
async def test_node_always_decomposes_on_research_backend(monkeypatch):
    mock_llm = AsyncMock(return_value=["x", "y", "z"])
    monkeypatch.setattr(decompose, "_call_llm", mock_llm)

    # A STALE single-item subtasks from the pre-route decompose_node must be ignored;
    # the orchestrator re-plans on the resolved research backend.
    state = {"task": "compare a/b/c", "subtasks": ["stale-single"], "backend": "deepseek_v4_flash"}
    out = await research_plan_node(state)

    assert out["subtasks"] == ["x", "y", "z"]          # re-decomposed, not the stale value
    assert out["research_tier"] == TIER_FANOUT
    # the re-plan path must still emit the system bookkeeping message (parity w/ test 2)
    assert any(m.get("role") == "system" for m in out["messages"])
    mock_llm.assert_awaited_once()
    # decomposed on the RESOLVED backend (route_node's), not "local" — asserted
    # signature-agnostically (positional OR kw), robust to a future _call_llm reorder.
    call = mock_llm.await_args
    passed_backend = (
        call.kwargs["backend"] if "backend" in call.kwargs
        else (call.args[1] if len(call.args) > 1 else None)
    )
    assert passed_backend == "deepseek_v4_flash"


# ── 4. single tier: no fanout_width, task unchanged ──────────────────────────
@pytest.mark.asyncio
async def test_single_tier_no_width_no_task_mutation(monkeypatch):
    mock_llm = AsyncMock(return_value=["only one"])
    monkeypatch.setattr(decompose, "_call_llm", mock_llm)

    state = {"task": "q", "backend": "local"}
    out = await research_plan_node(state)

    assert out["research_tier"] == TIER_SINGLE
    assert "fanout_width" not in out
    assert state["task"] == "q"  # never mutated


# ── 5. hierarchical tier (12 subtasks) ───────────────────────────────────────
@pytest.mark.asyncio
async def test_hierarchical_tier(monkeypatch):
    subtasks = [f"q{i}" for i in range(12)]
    mock_llm = AsyncMock(return_value=subtasks)
    monkeypatch.setattr(decompose, "_call_llm", mock_llm)

    out = await research_plan_node({"task": "q", "backend": "local"})
    assert out["research_tier"] == TIER_HIER
    assert "fanout_width" not in out
    assert len(out["subtasks"]) == 12


# ── 5b. fanout_width tracks the sub-question count across the fan-out band ────
@pytest.mark.parametrize("n", [2, 9])  # both ends of the 2..9 flat-fan-out band
@pytest.mark.asyncio
async def test_fanout_width_tracks_count(monkeypatch, n):
    mock_llm = AsyncMock(return_value=[f"sq{i}" for i in range(n)])
    monkeypatch.setattr(decompose, "_call_llm", mock_llm)
    out = await research_plan_node({"task": "q", "backend": "local"})
    assert out["research_tier"] == TIER_FANOUT
    assert out["fanout_width"] == n  # width == count (no off-by-one)


# ── 6. fail-open single (decompose returns [question]) ───────────────────────
@pytest.mark.asyncio
async def test_fail_open_single(monkeypatch):
    mock_llm = AsyncMock(return_value=["the question"])
    monkeypatch.setattr(decompose, "_call_llm", mock_llm)

    out = await research_plan_node({"task": "the question", "backend": "local"})
    assert out["research_tier"] == TIER_SINGLE  # never raises


# ── 7. _route_after_research_plan routing edge ───────────────────────────────
def test_route_after_research_plan():
    assert _route_after_research_plan({"research_tier": TIER_SINGLE, "subtasks": ["a"]}) == "execute"
    assert _route_after_research_plan({"research_tier": TIER_FANOUT, "subtasks": ["a", "b"]}) == "dispatch"
    assert _route_after_research_plan({"research_tier": TIER_HIER, "subtasks": list(range(10))}) == "manager_dispatch"
    # fail-loud (incl. the `state.get("subtasks") or []` normalization of falsy values)
    assert _route_after_research_plan({"error": "x", "subtasks": ["a"]}) == END
    assert _route_after_research_plan({"subtasks": []}) == END
    assert _route_after_research_plan({"subtasks": None}) == END
    assert _route_after_research_plan({}) == END
    # tier recomputed from len(subtasks) when research_tier absent (pure of state)
    assert _route_after_research_plan({"subtasks": ["a", "b", "c"]}) == "dispatch"           # 3 → fanout
    assert _route_after_research_plan({"subtasks": list(range(12))}) == "manager_dispatch"   # 12 → hier
    assert _route_after_research_plan({"subtasks": ["only"]}) == "execute"                   # 1 → single
    # defensive default: an unknown/invalid tier string falls through to execute (never
    # silently dispatch/manager_dispatch a swarm on a garbage tier).
    assert _route_after_research_plan({"research_tier": "bogus", "subtasks": list(range(20))}) == "execute"


# ── 8. _route_after_recall byte-identical guard + the research arm ────────────
def test_route_after_recall_research_arm_and_byte_identical():
    # the new arm
    assert _route_after_recall({"research_request": True}) == "research_plan"
    # absent / falsy / None ⇒ byte-identical to today
    assert _route_after_recall({}) == "dispatch"
    assert _route_after_recall({"research_request": False}) == "dispatch"
    assert _route_after_recall({"research_request": None}) == "dispatch"
    # every EXISTING arm still resolves exactly as before
    assert _route_after_recall({"build_request": True}) == "plan_contracts"
    assert _route_after_recall({"hierarchical": True}) == "manager_dispatch"
    assert _route_after_recall({"council_spec": {"mode": "fusion"}}) == "panel_dispatch"
    assert _route_after_recall({"post_condition": {"statement": "x"}}) == "postcondition"
    # co-set precedence: research_request is the orchestrator's (beats build_request)
    assert _route_after_recall({"research_request": True, "build_request": True}) == "research_plan"
    # but post_condition (a verification sub-step both lanes call) is checked FIRST → wins
    assert _route_after_recall(
        {"research_request": True, "post_condition": {"statement": "x"}}
    ) == "postcondition"
    # full precedence: research beats hierarchical + council too (order is
    # postcondition > research > build > hier > council > dispatch)
    assert _route_after_recall({"research_request": True, "hierarchical": True}) == "research_plan"
    assert _route_after_recall(
        {"research_request": True, "council_spec": {"mode": "fusion"}}
    ) == "research_plan"


# ── 9. graph wires research_plan + its edge targets exist (compiles) ─────────
def test_graph_wires_research_plan():
    nodes = set(build_graph(checkpointer=None).get_graph().nodes.keys())
    # research_plan AND its three conditional-edge targets must be registered nodes
    # (LangGraph validates edge destinations lazily at invoke, not at build, so a
    # future rename/removal of execute/dispatch/manager_dispatch would otherwise kill
    # a research turn at runtime with no test catching it).
    assert {"research_plan", "execute", "dispatch", "manager_dispatch"}.issubset(nodes)
