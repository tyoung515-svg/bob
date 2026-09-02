"""
BoBClaw Core — decompose_node council-skip tests (A-10 / G2 topology).

Since A-10, decompose runs AFTER route (START -> route -> decompose -> recall), so
the council decision is made ONCE in route (which sets ``council_spec``) and decompose
reads that authoritative signal — no more ``_is_council_turn`` predicate mirror.

These prove: a turn route flagged as council (``council_spec`` present) skips the
decompose LLM call entirely (the council branch never reads ``subtasks``, so
decomposing would only JIT-load a local model whose output is discarded — the
VRAM-churn bug); non-council turns keep decomposing exactly as before; and the
approval loop-back (route -> decompose re-entry) is idempotent. All LLM calls are
mocked — nothing live.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import core.nodes.decompose as decompose
from core.nodes.decompose import decompose_node
from core.nodes.route import route_node


COMPLEX_TASK = "Implement a full REST API with authentication, storage, and tests"


@pytest.fixture
def llm_spy(monkeypatch):
    """Replace the module-level _call_llm seam; fail loudly if ever awaited."""
    spy = AsyncMock(return_value=["a", "b"])
    monkeypatch.setattr(decompose, "_call_llm", spy)
    return spy


# ─── council turns (council_spec set by route) skip the LLM ────────────────────

@pytest.mark.asyncio
async def test_council_spec_present_skips_decompose(llm_spy):
    """route already set council_spec ⇒ decompose never pays for a decomposition
    the council branch won't read."""
    state = {
        "task": COMPLEX_TASK,
        "backend": "local",
        "face_id": "council-max",
        "council_spec": {"mode": "fusion", "seats": ["framer", "stress", "wildcard"]},
    }
    result = await decompose_node(state)

    llm_spy.assert_not_awaited()
    assert "subtasks" not in result
    assert "decomposition skipped" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_empty_council_spec_still_skips(llm_spy):
    """A present-but-empty council_spec ({} = council with default mode) is still a
    council turn — presence-based, mirroring graph._route_after_recall."""
    state = {
        "task": COMPLEX_TASK,
        "backend": "local",
        "face_id": "assistant",
        "council_spec": {},
    }
    result = await decompose_node(state)

    llm_spy.assert_not_awaited()
    assert "subtasks" not in result


# ─── non-council turns keep decomposing ───────────────────────────────────────

@pytest.mark.asyncio
async def test_no_council_spec_still_decomposes(llm_spy):
    """No council_spec ⇒ ordinary decomposition, unchanged."""
    state = {"task": COMPLEX_TASK, "backend": "local", "face_id": "assistant"}
    result = await decompose_node(state)

    llm_spy.assert_awaited_once()
    assert result.get("subtasks") == ["a", "b"]


@pytest.mark.asyncio
async def test_none_council_spec_still_decomposes(llm_spy):
    """An explicit None council_spec ⇒ ordinary decomposition (absent == None)."""
    state = {
        "task": COMPLEX_TASK,
        "backend": "local",
        "face_id": "assistant",
        "council_spec": None,
    }
    result = await decompose_node(state)

    llm_spy.assert_awaited_once()
    assert result.get("subtasks") == ["a", "b"]


# ─── approval-resume idempotency (route -> decompose re-entry) ─────────────────

@pytest.mark.asyncio
async def test_resume_with_subtasks_is_idempotent(llm_spy):
    """The approval loop-back re-enters decompose (approval -> route -> decompose).
    subtasks already present ⇒ do not re-call the LLM or re-append the card."""
    state = {
        "task": COMPLEX_TASK,
        "backend": "local",
        "face_id": "assistant",
        "subtasks": ["prior-1", "prior-2"],
    }
    result = await decompose_node(state)

    llm_spy.assert_not_awaited()
    assert result == {}  # no messages re-appended (reducer would duplicate)


# ─── behavioral: route's decision actually reaches decompose ──────────────────

@pytest.mark.asyncio
async def test_route_then_decompose_council_max_no_jit_load(llm_spy):
    """End-to-end of the head: run route_node then decompose_node for the council-max
    face. route sets council_spec; decompose reads it and never JIT-loads a model.
    Proves route-produced council_spec reaches decompose (Sol's behavioral gate)."""
    state = {
        "task": COMPLEX_TASK,
        "backend": "local",
        "face_id": "council-max",
        "model_override": None,
    }
    route_out = await route_node(state)
    assert route_out.get("council_spec") is not None  # route made the decision

    # Thread route's output into state (what the graph does between nodes).
    state.update(route_out)
    dec_out = await decompose_node(state)

    llm_spy.assert_not_awaited()
    assert "subtasks" not in dec_out
    assert "decomposition skipped" in dec_out["messages"][0]["content"]
