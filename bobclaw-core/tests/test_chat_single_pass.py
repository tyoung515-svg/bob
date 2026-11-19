"""
BoBClaw Core — chat turns answer in one pass.

Owner decision 2026-10-27: a conversational face must not decompose and fan out.
Before this, a "send out a research team" chat turn produced five planner subtasks,
five workers, and a join that concatenated them as "### Subtask 1..5" with no
synthesis step on that path — the operator read a work breakdown instead of an
answer. Fan-out stays reserved for explicit research/build/council turns and for
dispatched runs.

These tests pin the boundary from both sides: the chat face never decomposes and
never fans out, and a non-chat face still does both.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import core.nodes.decompose as decompose
from core.config import CHAT_SINGLE_PASS_FACES
from core.nodes.decompose import decompose_node
from core.nodes.dispatch import dispatch_node

COMPLEX_TASK = (
    "Research every open and closed weight model released in the last 30 days, "
    "compare them, and propose a monitoring schedule for the team"
)


@pytest.fixture
def llm_spy(monkeypatch):
    spy = AsyncMock(return_value=["a", "b", "c", "d", "e"])
    monkeypatch.setattr(decompose, "_call_llm", spy)
    return spy


def test_the_chat_face_is_registered_as_single_pass():
    assert "assistant" in CHAT_SINGLE_PASS_FACES


@pytest.mark.asyncio
async def test_chat_face_does_not_decompose(llm_spy):
    state = {"task": COMPLEX_TASK, "backend": "deepseek_v4_flash", "face_id": "assistant"}
    result = await decompose_node(state)

    llm_spy.assert_not_awaited()
    assert "subtasks" not in result
    assert "one pass" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_non_chat_face_still_decomposes(llm_spy):
    state = {"task": COMPLEX_TASK, "backend": "deepseek_v4_flash", "face_id": "researcher"}
    result = await decompose_node(state)

    llm_spy.assert_awaited_once()
    assert result.get("subtasks") == ["a", "b", "c", "d", "e"]


def test_chat_face_never_fans_out_even_with_subtasks_in_state():
    state = {
        "task": COMPLEX_TASK,
        "face_id": "assistant",
        "backend": "deepseek_v4_flash",
        "messages": [],
        "subtasks": ["a", "b", "c", "d", "e"],
        "fanout_width": None,
        "escalation_backend": "deepseek_v4_flash",
    }
    assert dispatch_node(state).get("fanout_subtasks") is None


def test_a_research_turn_still_fans_out():
    """Control: the same subtask count on a research face must still fan out."""
    state = {
        "task": COMPLEX_TASK,
        "face_id": "researcher",
        "backend": "deepseek_v4_flash",
        "messages": [],
        "subtasks": ["a", "b", "c", "d", "e"],
        "fanout_width": None,
        "escalation_backend": "deepseek_v4_flash",
    }
    fanout = dispatch_node(state).get("fanout_subtasks")
    assert fanout is not None and len(fanout) == 5
