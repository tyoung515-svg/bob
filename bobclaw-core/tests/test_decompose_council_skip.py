"""
BoBClaw Core — decompose_node council-skip tests.

Proves council-shaped turns (council-max face, or a profile with a council
``shape``) skip the decompose LLM call entirely: the council branch never
reads ``subtasks``, so decomposing first only JIT-loads a local model whose
output is discarded (the VRAM-churn bug).  Non-council turns must keep
decomposing exactly as before.  All LLM calls are mocked — nothing live.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import core.nodes.decompose as decompose
from core.nodes.decompose import decompose_node


COMPLEX_TASK = "Implement a full REST API with authentication, storage, and tests"


@pytest.fixture
def llm_spy(monkeypatch):
    """Replace the module-level _call_llm seam; fail loudly if ever awaited."""
    spy = AsyncMock(return_value=["a", "b"])
    monkeypatch.setattr(decompose, "_call_llm", spy)
    return spy


# ─── council turns skip the LLM ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_council_max_face_skips_decompose(llm_spy):
    """The council-max face never pays for a decomposition it won't read."""
    state = {"task": COMPLEX_TASK, "backend": "local", "face_id": "council-max"}
    result = await decompose_node(state)

    llm_spy.assert_not_awaited()
    assert "subtasks" not in result
    assert "decomposition skipped" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_council_shape_profile_skips_decompose(llm_spy, monkeypatch):
    """A profile that compiles to a council (has ``shape``) also skips."""
    monkeypatch.setattr(
        "core.teams.load_profile",
        lambda name: {"name": name, "shape": "fusion", "seats": []},
    )
    state = {
        "task": COMPLEX_TASK,
        "backend": "local",
        "face_id": "assistant",
        "profile_name": "spec-forge",
    }
    result = await decompose_node(state)

    llm_spy.assert_not_awaited()
    assert "subtasks" not in result


# ─── non-council turns keep decomposing ───────────────────────────────────────

@pytest.mark.asyncio
async def test_roster_profile_still_decomposes(llm_spy, monkeypatch):
    """A plain roster profile (no ``shape``) is NOT a council — decompose runs."""
    monkeypatch.setattr(
        "core.teams.load_profile",
        lambda name: {"name": name, "roles": {"worker": ["deepseek"]}},
    )
    state = {
        "task": COMPLEX_TASK,
        "backend": "local",
        "face_id": "assistant",
        "profile_name": "plain-roster",
    }
    result = await decompose_node(state)

    llm_spy.assert_awaited_once()
    assert result.get("subtasks") == ["a", "b"]


@pytest.mark.asyncio
async def test_plain_complex_task_still_decomposes(llm_spy):
    """No face/profile council signal — original behaviour unchanged."""
    state = {"task": COMPLEX_TASK, "backend": "local", "face_id": "assistant"}
    result = await decompose_node(state)

    llm_spy.assert_awaited_once()
    assert result.get("subtasks") == ["a", "b"]
