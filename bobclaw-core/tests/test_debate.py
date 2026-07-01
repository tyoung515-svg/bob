"""
BoBClaw Core — Debate council shape tests (network-free).

Grows phase-by-phase: D0 = the guard + config + routing; later phases add the
prior-positions context (D1), the convergence gate (D2), and the graph loop (D3).
"""
from __future__ import annotations

from core.graph import (
    _route_after_debate,
    _route_after_recall,
    _route_after_synthesize,
    build_graph,
)


# ── routing + config ─────────────────────────────────────────────────────────

def test_debate_and_fusion_route_to_panel_dispatch():
    """Debate shares the panel path with fusion; they diverge only at the close gate."""
    assert _route_after_recall({"council_spec": {"mode": "debate"}}) == "panel_dispatch"
    assert _route_after_recall({"council_spec": {"mode": "fusion"}}) == "panel_dispatch"
    assert _route_after_recall({"council_spec": {"mode": "sequential"}}) == "council"
    assert _route_after_recall({"council_spec": {}}) == "panel_dispatch"   # default fusion
    assert _route_after_recall({}) == "dispatch"                            # non-council


def test_council_max_rounds_config_present():
    from core.config import COUNCIL_MAX_ROUNDS, DEBATE_ROUND_USD
    assert isinstance(COUNCIL_MAX_ROUNDS, int) and COUNCIL_MAX_ROUNDS >= 1
    assert isinstance(DEBATE_ROUND_USD, float) and DEBATE_ROUND_USD > 0


# ── D1: seats see prior-round positions ──────────────────────────────────────

from core.nodes.panel import (  # noqa: E402
    _build_debate_context,
    _route_after_panel,
    panel_dispatch_node,
)


def test_debate_round0_is_blind_like_fusion():
    spec = {"mode": "debate", "seats": ["framer", "stress"], "synth_backend": "minimax"}
    out = panel_dispatch_node({"council_spec": spec, "task": "the topic", "council_round": 0})
    task = out["council_spec"]["panel_task"]
    assert "PRIOR ROUND POSITIONS" not in task   # round 0 = blind, like fusion
    assert "the topic" in task


def test_debate_round1_sees_prior_positions():
    spec = {"mode": "debate", "seats": ["framer", "stress"], "synth_backend": "minimax"}
    panel_results = [
        {"idx": 0, "posture": "framer", "text": "FRAMER_POSITION", "round": 0},
        {"idx": 1, "posture": "stress", "text": "STRESS_POSITION", "round": 0},
    ]
    out = panel_dispatch_node({"council_spec": spec, "task": "the topic",
                               "council_round": 1, "panel_results": panel_results})
    task = out["council_spec"]["panel_task"]
    assert "PRIOR ROUND POSITIONS" in task
    assert "[framer] argued:" in task and "FRAMER_POSITION" in task
    assert "[stress] argued:" in task and "STRESS_POSITION" in task


def test_route_after_panel_stamps_council_round_in_debate():
    spec = {"mode": "debate", "panel_task": "t", "resolved_seats": [
        {"idx": 0, "posture": "framer", "backend": "deepseek_v4_flash",
         "fallback_chain": [], "role_prompt": ""}]}
    sends = _route_after_panel({"council_spec": spec, "council_round": 2, "council_restart": 0})
    assert sends[0].arg["panel_round"] == 2   # council_round, not council_restart


def test_route_after_panel_stamps_council_restart_in_fusion():
    spec = {"mode": "fusion", "panel_task": "t", "resolved_seats": [
        {"idx": 0, "posture": "framer", "backend": "deepseek_v4_flash",
         "fallback_chain": [], "role_prompt": ""}]}
    sends = _route_after_panel({"council_spec": spec, "council_round": 2, "council_restart": 1})
    assert sends[0].arg["panel_round"] == 1   # council_restart (fusion unchanged)


def test_build_debate_context_round0_empty():
    assert _build_debate_context([], -1) == ""
    assert _build_debate_context(
        [{"idx": 0, "posture": "framer", "text": "x", "round": 0}], -1) == ""


def test_build_debate_context_filters_to_prev_round():
    pr = [{"idx": 0, "posture": "framer", "text": "OLD", "round": 0},
          {"idx": 0, "posture": "framer", "text": "NEW", "round": 1}]
    ctx = _build_debate_context(pr, 0)
    assert "OLD" in ctx and "NEW" not in ctx


# ── D2: convergence gate + bounds + exactly-once ─────────────────────────────

from unittest.mock import AsyncMock, patch  # noqa: E402

from core.nodes.debate import debate_converge_node, is_debate  # noqa: E402


class _Writer:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, chunk):
        self.calls.append(chunk)


def _patch_emit(writer):
    return [
        patch("core.nodes.synthesize._get_stream_writer", return_value=writer),
        patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()),
    ]


def _dstate(active_debate, *, council_round=0, pending="ANSWER", prev=None, cost=0.0,
            bounds=None):
    spec = {"mode": "debate", "synth_backend": "minimax"}
    if prev is not None:
        spec["prev_active_debate"] = prev
    if bounds is not None:
        spec["bounds"] = bounds
    return {
        "task": "t",
        "council_spec": spec,
        "council_round": council_round,
        "council_cost_usd": cost,
        "council_handoff": {"active_debate": active_debate, "resolved": [],
                            "blocked": [], "corrections": [], "next_task": ""},
        "council_pending_answer": ({"content": pending, "backend": "minimax"} if pending else None),
        "messages": [{"role": "user", "content": "q"}],
    }


async def _run_converge(st):
    w = _Writer()
    p1, p2 = _patch_emit(w)
    with p1, p2:
        out = await debate_converge_node(st)
    return out, w


def test_is_debate():
    assert is_debate({"mode": "debate"}) is True
    assert is_debate({"mode": "fusion"}) is False
    assert is_debate({}) is False and is_debate(None) is False


async def test_debate_converges_on_empty_active_debate():
    out, w = await _run_converge(_dstate([]))
    assert "council_round" not in out                       # converge, no increment
    assert out["messages"] == [{"role": "assistant", "content": "ANSWER"}]
    assert out["council_pending_answer"] is None
    assert len(w.calls) == 1


async def test_debate_converges_on_no_delta():
    out, w = await _run_converge(_dstate(["IDEA-01"], prev=["IDEA-01"]))
    assert "council_round" not in out
    assert len(w.calls) == 1


async def test_debate_loops_on_changed_debate():
    out, w = await _run_converge(_dstate(["IDEA-01", "IDEA-02"], prev=["IDEA-01"],
                                         council_round=0))
    assert out["council_round"] == 1
    spec = out["council_spec"]
    assert spec["debate_continue"] is True
    assert spec["prev_active_debate"] == ["IDEA-01", "IDEA-02"]
    assert "panel_task" not in spec and "resolved_seats" not in spec  # cleared for rebuild
    assert "messages" not in out                            # NO commit on a loop
    assert w.calls == []


async def test_debate_round_cap_forces_converge():
    # round 2 with max_rounds 3 → round_idx+1 == 3 → converge despite live debate.
    out, w = await _run_converge(_dstate(["IDEA-01"], prev=["IDEA-02"],
                                         council_round=2, bounds={"max_rounds": 3}))
    assert "council_round" not in out
    assert len(w.calls) == 1


async def test_debate_cost_ceiling_fails_loud():
    out, w = await _run_converge(_dstate(["IDEA-01"], prev=["IDEA-02"],
                                         council_round=0, cost=0.15,
                                         bounds={"max_usd": 0.2}))
    assert out["error"] and "ceiling" in out["error"].lower()
    assert "council_round" not in out                       # converge
    assert len(w.calls) == 1                                # best answer committed once


async def test_debate_bounds_fallback_to_global_max_rounds():
    # No bounds → global COUNCIL_MAX_ROUNDS (3); a changed debate at round 0 loops.
    out, _ = await _run_converge(_dstate(["IDEA-01"], prev=["IDEA-02"], council_round=0))
    assert out["council_round"] == 1


_HANDOFF = ("\n\n### 📋 COUNCIL HANDOFF\n- **[ACTIVE DEBATE]:** {debate}\n"
            "- **[NEXT TASK]:** @framer")


async def test_debate_exactly_once_across_rounds():
    """The critical contract: synthesize DEFERS every round in debate mode;
    debate_converge is the SOLE emitter and commits exactly once on converge. A
    2-round debate emits exactly ONE answer (round 1's), never round 0's."""
    from core.nodes.synthesize import synthesize_node

    w = _Writer()
    state = {
        "task": "t",
        "council_spec": {"mode": "debate", "synth_backend": "minimax"},
        "council_round": 0,
        "council_cost_usd": 0.0,
        "council_handoff": None,
        "messages": [{"role": "user", "content": "q"}],
        "panel_results": [{"idx": 0, "posture": "framer", "backend": "x",
                           "text": "v", "round": 0}],
        "council_pending_answer": None,
    }
    synth_outputs = iter([
        "ROUND0 answer" + _HANDOFF.format(debate="IDEA-01, IDEA-02"),
        "ROUND1 answer" + _HANDOFF.format(debate="none"),
    ])

    async def _synth(messages, backend):
        return next(synth_outputs)

    def _apply(delta):
        for k, v in delta.items():
            if k == "messages":
                state["messages"] = (state.get("messages") or []) + list(v)
            else:
                state[k] = v

    with patch("core.nodes.synthesize._send_to_backend", _synth), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=w):
        # round 0: synth defers (debate mode), commits nothing
        _apply(await synthesize_node(state))
        assert state["council_pending_answer"]["content"].startswith("ROUND0")
        assert w.calls == []
        # round 0 converge: active IDEA-01,IDEA-02 vs prev empty → changed → loop
        _apply(await debate_converge_node(state))
        assert state["council_round"] == 1
        assert w.calls == []                                # still nothing emitted
        # round 1: synth overwrites the carrier
        _apply(await synthesize_node(state))
        assert state["council_pending_answer"]["content"].startswith("ROUND1")
        # round 1 converge: active empty → converge → commit ROUND1 once
        _apply(await debate_converge_node(state))

    assert len(w.calls) == 1
    assert w.calls[0]["content"].startswith("ROUND1")
    assistants = [m for m in state["messages"]
                  if isinstance(m, dict) and m.get("role") == "assistant"]
    assert assistants == [{"role": "assistant", "content": w.calls[0]["content"]}]
    assert all("ROUND0" not in (m.get("content") or "") for m in state["messages"])
    assert state["council_pending_answer"] is None


# ── D3: graph wiring + close-gate routing ────────────────────────────────────

def test_route_after_synthesize_picks_gate_by_mode():
    assert _route_after_synthesize({"council_spec": {"mode": "debate"}}) == "debate_converge"
    assert _route_after_synthesize({"council_spec": {"mode": "fusion"}}) == "ground"
    assert _route_after_synthesize({"council_spec": {}}) == "ground"      # default fusion
    assert _route_after_synthesize({}) == "ground"


def test_route_after_debate_loops_or_ends():
    from langgraph.graph import END
    assert _route_after_debate({"council_spec": {"debate_continue": True}}) == "panel_dispatch"
    assert _route_after_debate({"council_spec": {}}) == END
    assert _route_after_debate({}) == END


def test_graph_wires_debate_converge_not_guard():
    nodes = set(build_graph().get_graph().nodes.keys())
    assert "debate_converge" in nodes and "debate_guard" not in nodes
    assert {"panel_dispatch", "synthesize", "council", "ground"}.issubset(nodes)


async def test_debate_full_loop_routes_then_ends():
    """Drive synthesize → _route_after_synthesize → debate_converge → _route_after_debate
    with the REAL routing helpers (sub-loop, not ainvoke-from-START, whose
    decompose/route/recall LLM nodes are brittle to mock — same approach as the
    grounding loop test): a 2-round debate routes panel_dispatch (round-0 loop) then
    END (round-1 converge), emitting exactly one answer (round 1's)."""
    from langgraph.graph import END

    from core.nodes.synthesize import synthesize_node

    w = _Writer()
    state = {
        "task": "t",
        "council_spec": {"mode": "debate", "synth_backend": "minimax"},
        "council_round": 0,
        "council_cost_usd": 0.0,
        "council_handoff": None,
        "messages": [{"role": "user", "content": "q"}],
        "panel_results": [{"idx": 0, "posture": "framer", "backend": "x",
                           "text": "v", "round": 0}],
        "council_pending_answer": None,
    }
    synth_outputs = iter([
        "ROUND0" + _HANDOFF.format(debate="IDEA-01, IDEA-02"),
        "ROUND1" + _HANDOFF.format(debate="none"),
    ])

    async def _synth(messages, backend):
        return next(synth_outputs)

    def _apply(delta):
        for k, v in delta.items():
            if k == "messages":
                state["messages"] = (state.get("messages") or []) + list(v)
            else:
                state[k] = v

    routes = []
    with patch("core.nodes.synthesize._send_to_backend", _synth), \
         patch("core.nodes.synthesize._append_agent_turn_event", AsyncMock()), \
         patch("core.nodes.synthesize._get_stream_writer", return_value=w):
        _apply(await synthesize_node(state))
        assert _route_after_synthesize(state) == "debate_converge"   # mode picks the gate
        _apply(await debate_converge_node(state))
        routes.append(_route_after_debate(state))                    # round-0 → loop
        _apply(await synthesize_node(state))
        _apply(await debate_converge_node(state))
        routes.append(_route_after_debate(state))                    # round-1 → END

    assert routes == ["panel_dispatch", END]
    assert len(w.calls) == 1 and w.calls[0]["content"].startswith("ROUND1")
