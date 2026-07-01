"""
BoBClaw Core — Unit tests for dispatch_node (fan-out state-mutation node)
and _route_after_dispatch (conditional edge).

dispatch_node returns a dict (state delta).  _route_after_dispatch reads
the updated state and returns a routing decision (string or list[Send]).
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


def _dispatch_and_route(**overrides):
    """Helper: run dispatch_node then pipe through _route_after_dispatch."""
    st = _state(**overrides)
    delta = dispatch_node(st)
    st.update(delta)
    return _route_after_dispatch(st)


# ─── dispatch_node state assertions ─────────────────────────────────────

def test_dispatch_no_fanout_below_threshold():
    """4 subtasks is below the threshold — no fan-out."""
    result = dispatch_node(_state(subtasks=["a", "b", "c", "d"]))
    assert result.get("fanout_subtasks") is None


def test_dispatch_fanout_at_threshold():
    """5 subtasks triggers fan-out — fanout_subtasks is populated."""
    result = dispatch_node(_state(subtasks=["a", "b", "c", "d", "e"]))
    fanout = result.get("fanout_subtasks")
    assert fanout is not None
    assert len(fanout) == 5
    assert [e["text"] for e in fanout] == ["a", "b", "c", "d", "e"]


def test_dispatch_explicit_width_forces_fanout():
    """fanout_width=3 with 4 subtasks produces 3 fanout entries."""
    result = dispatch_node(_state(subtasks=["a", "b", "c", "d"], fanout_width=3))
    fanout = result.get("fanout_subtasks")
    assert fanout is not None
    assert len(fanout) == 3
    assert [e["text"] for e in fanout] == ["a", "b", "c"]


def test_dispatch_width_one_disables_fanout():
    """fanout_width=1 explicitly disables fan-out even with many subtasks."""
    result = dispatch_node(_state(
        subtasks=["a", "b", "c", "d", "e"],
        fanout_width=1,
    ))
    assert result.get("fanout_subtasks") is None


def test_dispatch_workspace_bound_bypasses():
    """Workspace-bound workers (worker-opencode) never fan out."""
    result = dispatch_node(_state(
        face_id="worker-opencode",
        backend="opencode_serve",
        subtasks=["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"],
    ))
    assert result.get("fanout_subtasks") is None


def test_dispatch_single_subtask_never_fans_out():
    """A single subtask never fans out, even with explicit width override."""
    result = dispatch_node(_state(
        subtasks=["only one"],
        fanout_width=5,
    ))
    assert result.get("fanout_subtasks") is None


# ─── _route_after_dispatch routing assertions ───────────────────────────

def test_dispatch_routes_to_execute_below_threshold():
    """Below threshold routes to execute."""
    route = _dispatch_and_route(subtasks=["a", "b", "c", "d"])
    assert route == "execute"


def test_dispatch_routes_to_worker_at_threshold():
    """At threshold routes to list[Send]."""
    route = _dispatch_and_route(subtasks=["a", "b", "c", "d", "e"])
    assert isinstance(route, list)
    assert len(route) == 5
    for item in route:
        assert isinstance(item, Send)
        assert item.node == "worker"
    texts = [s.arg["task"] for s in route]
    assert texts == ["a", "b", "c", "d", "e"]
    phases = [s.arg.get("phase") for s in route]
    assert all(p == "dispatch" for p in phases)


def test_dispatch_route_explicit_width_three():
    """fanout_width=3 routes 3 Sends."""
    route = _dispatch_and_route(subtasks=["a", "b", "c", "d"], fanout_width=3)
    assert isinstance(route, list)
    assert len(route) == 3
    texts = [s.arg["task"] for s in route]
    assert texts == ["a", "b", "c"]
