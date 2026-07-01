"""
BoBClaw Core — Unit tests for fan-out pre-flight combined approval (handoff 006)

Tests cover:
  - Approval required pauses dispatch (routes to approval_node)
  - Approval message names all needing subtasks
  - Approval granted proceeds to fan-out (Sends emitted on re-entry)
  - No approval needed skips the pause entirely
"""
from __future__ import annotations

from core.nodes.dispatch import _route_after_dispatch, dispatch_node


def _state(**overrides) -> dict:
    base = {
        "task": "main",
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
    """Run dispatch_node then pipe through _route_after_dispatch."""
    st = _state(**overrides)
    delta = dispatch_node(st)
    st.update(delta)
    return _route_after_dispatch(st)


# ─── Approval pauses dispatch ────────────────────────────────────────────

def test_approval_required_pauses_dispatch():
    """When some subtasks need approval, dispatch routes through approval_node."""
    # "send email" matches _DANGEROUS_PATTERNS
    subtasks = [
        "summarize logs",
        "send email to bob",
        "list files",
        "generate report",
        "send email to alice",
    ]
    route = _dispatch_and_route(subtasks=subtasks)

    # Should route to approval, not directly to workers
    assert route == "approval"

    # The state delta should contain the approval message and flag
    st = _state(subtasks=subtasks)
    delta = dispatch_node(st)
    assert delta.get("approval_required") is True
    msgs = delta.get("messages", [])
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert "send email to bob" in content
    assert "send email to alice" in content


# ─── Approval message names all needing subtasks ─────────────────────────

def test_approval_message_names_all_needing_subtasks():
    """Assert the approval message lists all subtask indices that need approval."""
    subtasks = [
        "safe task",
        "send email to bob",
        "safe too",
        "rm -rf /tmp/test",
        "another safe",
    ]
    delta = dispatch_node(_state(subtasks=subtasks))
    assert delta.get("approval_required") is True
    content = delta["messages"][0]["content"]
    # Should reference subtask 2 (send email) and subtask 4 (rm -rf)
    assert "subtask 2" in content.lower() or "subtask 2" in content
    assert "subtask 4" in content.lower() or "subtask 4" in content


# ─── Approval granted proceeds to fan-out ────────────────────────────────

def test_approval_granted_proceeds_to_fanout():
    """After approval is granted, dispatch emits the full N Sends."""
    subtasks = [
        "summarize logs",
        "send email to bob",
        "list files",
        "run tests",
        "generate report",
    ]
    # Simulate re-entry after approval: approval_response is set
    route = _dispatch_and_route(
        subtasks=subtasks,
        approval_response="approve",
    )
    from langgraph.types import Send
    assert isinstance(route, list)
    assert len(route) == 5
    for item in route:
        assert isinstance(item, Send)
        assert item.node == "worker"


# ─── No approval needed skips pause ──────────────────────────────────────

def test_no_approval_needed_skips_pause():
    """Safe subtasks go straight to fan-out without entering approval_node."""
    subtasks = [
        "summarize logs",
        "list files",
        "generate report",
        "run tests",
        "clean up workspace",
    ]
    route = _dispatch_and_route(subtasks=subtasks)
    from langgraph.types import Send
    assert isinstance(route, list)
    assert len(route) == 5

    # dispatch_node should NOT set approval_required
    delta = dispatch_node(_state(subtasks=subtasks))
    assert delta.get("approval_required") is None or delta.get("approval_required") is False
