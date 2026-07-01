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

def test_route_after_join_loops_to_dispatch_when_waves_remain():
    from core.graph import _route_after_join

    # 25 subtasks on kimi_code (cap 10): after wave 0, (0+1)*10 < 25 → more waves.
    st = {"fanout_wave": 0, "backend": "kimi_code",
          "subtasks": [f"t{i}" for i in range(25)]}
    assert _route_after_join(st) == "dispatch"


def test_route_after_join_ends_on_final_wave():
    from langgraph.graph import END
    from core.graph import _route_after_join

    # Last wave: (2+1)*10 >= 25 → no more waves; no approval pending → END.
    st = {"fanout_wave": 2, "backend": "kimi_code",
          "subtasks": [f"t{i}" for i in range(25)]}
    assert _route_after_join(st) == END


def test_route_after_join_no_wave_state_ends():
    from langgraph.graph import END
    from core.graph import _route_after_join

    assert _route_after_join({}) == END
