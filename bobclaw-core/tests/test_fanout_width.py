"""
BoBClaw Core — Unit tests for fan-out width caps and wave-chunking (handoff 007)

Tests cover:
  - Single wave when subtasks are under the per-backend cap
  - Multiple waves when subtasks exceed the per-backend cap
  - Abort when subtasks exceed the global cap
"""
from __future__ import annotations

from langgraph.types import Send

from core.nodes.dispatch import _route_after_dispatch, dispatch_node


def _state(**overrides) -> dict:
    base = {
        "task": "implement the thing",
        "face_id": "worker-kimi",
        "backend": "kimi_code",
        "messages": [],
        "subtasks": None,
        "fanout_width": None,
        "escalation_backend": "kimi_platform",
    }
    base.update(overrides)
    return base


def test_width_under_per_backend_cap_no_chunking():
    """8 subtasks on claude_api (cap=20) → single wave, 8 Sends."""
    subtasks = [f"task {i}" for i in range(8)]
    st = _state(subtasks=subtasks, backend="claude_api")
    delta = dispatch_node(st)
    st.update(delta)
    route = _route_after_dispatch(st)

    assert isinstance(route, list)
    assert len(route) == 8
    for item in route:
        assert isinstance(item, Send)
    assert delta.get("fanout_wave") is None  # no wave state written by dispatch


def test_width_over_per_backend_cap_chunks_into_waves():
    """25 subtasks on kimi_code (cap=10) → 3 waves of 10/10/5."""
    subtasks = [f"task {i}" for i in range(25)]

    # ── Wave 0: indices 0..9 ──
    st = _state(subtasks=subtasks, backend="kimi_code")
    delta = dispatch_node(st)
    st.update(delta)
    route = _route_after_dispatch(st)
    assert isinstance(route, list)
    assert len(route) == 10
    indices = [s.arg["subtask_idx"] for s in route]
    assert indices == list(range(10))
    texts = [s.arg["task"] for s in route]
    assert texts == [f"task {i}" for i in range(10)]

    # ── Wave 1: indices 10..19 (re-entry via join setting fanout_wave=1) ──
    st["fanout_wave"] = 1
    delta = dispatch_node(st)
    st.update(delta)
    route = _route_after_dispatch(st)
    assert isinstance(route, list)
    assert len(route) == 10
    indices = [s.arg["subtask_idx"] for s in route]
    assert indices == list(range(10, 20))

    # ── Wave 2: indices 20..24 (re-entry with fanout_wave=2) ──
    st["fanout_wave"] = 2
    delta = dispatch_node(st)
    st.update(delta)
    route = _route_after_dispatch(st)
    assert isinstance(route, list)
    assert len(route) == 5
    indices = [s.arg["subtask_idx"] for s in route]
    assert indices == list(range(20, 25))


def test_width_over_global_cap_aborts():
    """150 subtasks exceed global cap 100 → error, no Sends."""
    subtasks = [f"task {i}" for i in range(150)]
    result = dispatch_node(_state(subtasks=subtasks))
    assert "error" in result
    assert "100" in result["error"]
    assert result.get("fanout_subtasks") is None


# ── _route_after_join: the wave re-entry decision (regression: it referenced
#    MAX_FANOUT_WIDTH_BY_BACKEND that was only imported inside create_graph, so any
#    wave-continuation call raised NameError — uncaught because nothing exercised it).

def test_route_after_join_loops_to_dispatch_when_join_signals_continue():
    from core.graph import _route_after_join

    # New contract: _route_after_join OBEYS join_node's fanout_continue signal — it does
    # NOT re-derive from fanout_wave (join already advanced it, which double-advanced the
    # old derivation and dropped the last wave). Intermediate wave → continue.
    assert _route_after_join({"fanout_continue": True}) == "dispatch"


def test_route_after_join_ends_when_join_signals_done():
    from langgraph.graph import END
    from core.graph import _route_after_join

    # Final wave: join_node set fanout_continue=False → no more waves → END.
    assert _route_after_join({"fanout_continue": False}) == END
    # Absent (single-wave / never chunked) → END too (no stale-True loop).
    assert _route_after_join({}) == END


def test_route_after_join_ignores_stale_fanout_wave():
    from langgraph.graph import END
    from core.graph import _route_after_join

    # A non-None fanout_wave must NOT by itself trigger another dispatch — the signal is
    # fanout_continue. (This is the exact shape that made the old code loop one wave short.)
    assert _route_after_join(
        {"fanout_wave": 4, "backend": "deepseek_v4_flash",
         "subtasks": [f"t{i}" for i in range(100)], "fanout_continue": False}
    ) == END


def test_route_after_join_no_wave_state_ends():
    from langgraph.graph import END
    from core.graph import _route_after_join

    assert _route_after_join({}) == END


# ── Integrated multi-wave loop ────────────────────────────────────────────────
# The regression that would have caught the off-by-one: the isolation tests above
# hand-set fanout_wave and never exercise the join_node-writes → _route_after_join-reads
# composition inside a real compiled loop. This drives dispatch → worker → join →
# _route_after_join across ALL waves and asserts every subtask ran and exactly one
# fleet_join closed the run (the pre-fix code ran only N-cap workers and emitted zero
# fleet_join, because the final emitting wave was skipped).

async def _run_multiwave(n: int, backend: str) -> list[dict]:
    from unittest.mock import patch

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph

    from core.graph import AgentState, _route_after_join
    from core.nodes.join import join_node
    from core.nodes.worker import worker_node

    async def _fake(messages, be, *a, **k):  # network-free worker
        return "ok"

    g = StateGraph(AgentState)
    g.add_node("dispatch", dispatch_node)
    g.add_node("worker", worker_node)
    g.add_node("join", join_node)
    g.add_edge(START, "dispatch")
    g.add_conditional_edges(
        "dispatch", _route_after_dispatch, {"execute": END, "approval": END, END: END},
    )
    g.add_edge("worker", "join")
    g.add_conditional_edges(
        "join", _route_after_join,
        {"dispatch": "dispatch", "approval": END, "verify": END, END: END},
    )
    graph = g.compile(checkpointer=MemorySaver())

    state = {
        "task": "gate", "face_id": "assistant", "backend": backend,
        "escalation_backend": None,
        "subtasks": [f"t{i}" for i in range(n)], "fanout_width": n,
        "fanout_wave": 0, "flight_id": "wave-test",
        "messages": [], "worker_results": [],
    }
    cfg = {"configurable": {"thread_id": f"wave-{backend}-{n}"}}
    custom: list[dict] = []
    with patch("core.nodes.worker._send_to_backend", _fake):
        async for mode, chunk in graph.astream(
            state, cfg, stream_mode=["custom", "updates"]
        ):
            if mode == "custom" and isinstance(chunk, dict):
                custom.append(chunk)
    return custom


def _tally(custom: list[dict]) -> tuple[list, list, list]:
    running = [c for c in custom
               if c.get("type") == "worker_state" and c.get("status") == "running"]
    joins = [c for c in custom if c.get("type") == "fleet_join"]
    starts = [c for c in custom if c.get("type") == "fleet_start"]
    return running, joins, starts


async def test_integrated_multiwave_runs_all_subtasks_kimi():
    """25 subtasks on kimi_code (cap 10) → 3 waves 10/10/5; ALL 25 run, exactly ONE join."""
    running, joins, starts = _tally(await _run_multiwave(25, "kimi_code"))
    assert len(running) == 25, f"expected 25 workers across waves, got {len(running)}"
    assert len(starts) == 3, f"expected 3 waves (10/10/5), got {len(starts)}"
    assert len(joins) == 1, f"expected exactly one final fleet_join, got {len(joins)}"
    assert joins[0]["total"] == 25 and joins[0]["ok"] == 25


async def test_integrated_multiwave_runs_all_subtasks_deepseek_100():
    """100 subtasks on deepseek_v4_flash (cap 20) → 5 waves; ALL 100 run, ONE join.
    The exact shape the FU3 live run exercised at 100 real agents."""
    running, joins, starts = _tally(await _run_multiwave(100, "deepseek_v4_flash"))
    assert len(running) == 100, (
        f"expected 100 workers, got {len(running)} — the off-by-one dropped a wave"
    )
    assert len(starts) == 5, f"expected 5 waves, got {len(starts)}"
    assert len(joins) == 1, f"expected exactly one final fleet_join, got {len(joins)}"
    assert joins[0]["total"] == 100 and joins[0]["ok"] == 100


async def test_integrated_multiwave_boundary_exact_multiple():
    """40 subtasks on deepseek (cap 20) → exactly 2 full waves, no phantom 3rd wave."""
    running, joins, starts = _tally(await _run_multiwave(40, "deepseek_v4_flash"))
    assert len(running) == 40
    assert len(starts) == 2, f"expected exactly 2 waves for 40/20, got {len(starts)}"
    assert len(joins) == 1 and joins[0]["total"] == 40
