"""
BoBClaw Core — Unit tests for fan-out state-shape contracts and sub-state isolation
"""
from __future__ import annotations

import operator
from typing import Annotated
from unittest.mock import AsyncMock, patch

import pytest
from langgraph.graph import StateGraph
from typing_extensions import TypedDict

from core.nodes.join import join_node
from core.nodes.worker import worker_node


# ─── Module-level test state types (needed for Python 3.14+ get_type_hints) ───

class _WorkerResultsTestState(TypedDict):
    worker_results: Annotated[list[dict], operator.add]


class _ArtifactsTestState(TypedDict):
    artifacts: Annotated[list[dict], operator.add]


# ─── worker_results reducer ───────────────────────────────────────────────────

def test_worker_results_reducer_concatenates():
    """operator.add reducer: two state deltas with single-entry lists merge to length-2."""
    builder = StateGraph(_WorkerResultsTestState)
    builder.add_node("a", lambda s: {"worker_results": [{"idx": 0}]})
    builder.add_node("b", lambda s: {"worker_results": [{"idx": 1}]})
    builder.add_edge("a", "b")
    builder.set_entry_point("a")
    builder.set_finish_point("b")
    graph = builder.compile()

    result = graph.invoke({"worker_results": []})
    assert len(result["worker_results"]) == 2


# ─── join_node sorting ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_results_sorted_by_idx_in_join():
    """join_node sorts worker_results by idx regardless of insertion order."""
    state = {
        "worker_results": [
            {"idx": 2, "text": "c", "status": "ok", "content": "result-c"},
            {"idx": 0, "text": "a", "status": "ok", "content": "result-a"},
            {"idx": 1, "text": "b", "status": "ok", "content": "result-b"},
        ],
    }
    result = await join_node(state)
    content = result["messages"][0]["content"]
    # Subtask sections should appear in order 1, 2, 3
    assert content.index("result-a") < content.index("result-b")
    assert content.index("result-b") < content.index("result-c")


# ─── artifacts reducer ───────────────────────────────────────────────────────

def test_artifacts_reducer_concatenates():
    """operator.add on artifacts: parallel writes merge cleanly."""
    builder = StateGraph(_ArtifactsTestState)
    builder.add_node("a", lambda s: {"artifacts": [{"file": "a.txt"}]})
    builder.add_node("b", lambda s: {"artifacts": [{"file": "b.txt"}]})
    builder.add_edge("a", "b")
    builder.set_entry_point("a")
    builder.set_finish_point("b")
    graph = builder.compile()

    result = graph.invoke({"artifacts": []})
    assert len(result["artifacts"]) == 2
    assert {"file": "a.txt"} in result["artifacts"]
    assert {"file": "b.txt"} in result["artifacts"]


# ─── Sub-state isolation ──────────────────────────────────────────────────────

def test_substate_does_not_carry_full_state():
    """The sub-state dict passed via Send should have only the locked keys."""
    from core.nodes.dispatch import _route_after_dispatch, dispatch_node

    state = {
        "task": "main task",
        "face_id": "worker-kimi",
        "backend": "kimi_code",
        "messages": [],
        "subtasks": ["a", "b", "c", "d", "e"],
        "fanout_width": None,
        "escalation_backend": "kimi_platform",
        "tools_allowed": ["code"],
        "approval_required": False,
        "error": None,
        "artifacts": [],
    }
    delta = dispatch_node(state)
    state.update(delta)
    route = _route_after_dispatch(state)
    assert isinstance(route, list)
    for send in route:
        sub = send.arg
        # Must have the locked keys
        assert "task" in sub
        assert "face_id" in sub
        assert "backend" in sub
        assert "escalation_backend" in sub
        assert "subtask_idx" in sub
        assert "messages" in sub
        # Must NOT carry full AgentState fields
        assert "tools_allowed" not in sub
        assert "approval_required" not in sub
        assert "artifacts" not in sub
        assert "worker_results" not in sub
        assert "subtasks" not in sub


# ─── join_node message contract ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_join_writes_single_assistant_message():
    """join_node produces exactly one assistant message regardless of worker count."""
    state = {
        "worker_results": [
            {"idx": 0, "text": "a", "status": "ok", "content": "A done"},
            {"idx": 1, "text": "b", "status": "ok", "content": "B done"},
            {"idx": 2, "text": "c", "status": "ok", "content": "C done"},
        ],
    }
    result = await join_node(state)
    msgs = result.get("messages", [])
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"


# ─── Best-effort 006 failure policy ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_join_best_effort_failure_006():
    """# 006 best-effort policy
    Only ALL workers failing sets state['error']; partial failures surface
    in the message but not as a turn-level error."""
    # Partial failure: 1 of 2 failed — no turn-level error
    state_partial = {
        "worker_results": [
            {"idx": 0, "text": "a", "status": "ok", "content": "ok"},
            {"idx": 1, "text": "b", "status": "failed", "error": "timeout"},
        ],
    }
    result = await join_node(state_partial)
    assert result.get("error") is None, "partial failure should not set error"
    assert "ok" in result["messages"][0]["content"]

    # All failed: turn-level error set
    state_all_fail = {
        "worker_results": [
            {"idx": 0, "text": "a", "status": "failed", "error": "timeout"},
            {"idx": 1, "text": "b", "status": "failed", "error": "crash"},
        ],
    }
    result2 = await join_node(state_all_fail)
    assert result2.get("error") is not None
    assert "timeout" in result2["error"]
    assert "crash" in result2["error"]
