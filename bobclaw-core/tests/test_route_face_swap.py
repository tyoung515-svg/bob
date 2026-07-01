"""
BoBClaw Core — Unit tests for _select_face heuristic in route_node

No I/O; purely synchronous logic over AgentState dicts.
"""
from __future__ import annotations

import pytest

from core.nodes.route import _select_face


# ─── Helpers ──────────────────────────────────────────────────────────────────

async def _select(state: dict) -> tuple[str | None, str | None]:
    """Await the async _select_face and return (new_face, old_face)."""
    return await _select_face(state)  # type: ignore[arg-type]


# ─── Positive: planning intent ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_code_shaped_planning_selects_planner_kimi():
    state = {"task": "plan a refactor of the auth module"}
    new_face, old_face = await _select(state)
    assert new_face == "planner-kimi"
    assert old_face == "assistant"


@pytest.mark.asyncio
async def test_design_api_selects_planner_kimi():
    state = {"task": "design the REST API endpoints for user profiles"}
    new_face, old_face = await _select(state)
    assert new_face == "planner-kimi"


@pytest.mark.asyncio
async def test_concept_planning_selects_planner_claude():
    state = {"task": "plan the product roadmap for Q3"}
    new_face, old_face = await _select(state)
    assert new_face == "planner-minimax"
    assert old_face == "assistant"


@pytest.mark.asyncio
async def test_architect_without_code_selects_planner_claude():
    state = {"task": "architect a scalable notification system"}
    new_face, old_face = await _select(state)
    assert new_face == "planner-minimax"


# ─── Positive: dispatch / phase triggers ──────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_subtask_selects_worker_kimi():
    state = {"task": "implement login", "dispatch_subtask": {"file": "auth.py"}}
    new_face, old_face = await _select(state)
    assert new_face == "worker-deepseek"


@pytest.mark.asyncio
async def test_dispatch_subtask_empty_dict_selects_worker_kimi():
    state = {"task": "build it", "dispatch_subtask": {}}
    new_face, old_face = await _select(state)
    assert new_face == "worker-deepseek"
    assert old_face == "assistant"


@pytest.mark.asyncio
async def test_dispatch_subtask_none_does_not_swap():
    state = {"task": "build it", "dispatch_subtask": None}
    new_face, old_face = await _select(state)
    assert new_face is None
    assert old_face is None


@pytest.mark.asyncio
async def test_phase_dispatch_selects_worker_kimi():
    state = {"task": "build the widget", "phase": "dispatch"}
    new_face, old_face = await _select(state)
    assert new_face == "worker-deepseek"


@pytest.mark.asyncio
async def test_phase_execute_selects_worker_kimi():
    state = {"task": "run tests", "phase": "execute"}
    new_face, old_face = await _select(state)
    assert new_face == "worker-deepseek"


@pytest.mark.asyncio
async def test_phase_build_selects_worker_kimi():
    state = {"task": "compile assets", "phase": "build"}
    new_face, old_face = await _select(state)
    assert new_face == "worker-deepseek"


@pytest.mark.asyncio
async def test_phase_worker_selects_worker_kimi():
    state = {"task": "deploy service", "phase": "worker"}
    new_face, old_face = await _select(state)
    assert new_face == "worker-deepseek"


@pytest.mark.asyncio
async def test_dispatch_with_many_subtasks_upgrades_to_worker_kimi_bulk():
    state = {
        "task": "implement",
        "dispatch_subtask": {"file": "x.py"},
        "subtasks": ["a", "b", "c", "d", "e"],  # 5 == threshold
    }
    new_face, _ = await _select(state)
    assert new_face == "worker-deepseek"


@pytest.mark.asyncio
async def test_dispatch_below_threshold_stays_on_worker_kimi():
    state = {
        "task": "implement",
        "dispatch_subtask": {"file": "x.py"},
        "subtasks": ["a", "b", "c"],  # 3 < threshold
    }
    new_face, _ = await _select(state)
    assert new_face == "worker-deepseek"


@pytest.mark.asyncio
async def test_phase_with_many_subtasks_upgrades_to_worker_kimi_bulk():
    state = {
        "task": "build",
        "phase": "execute",
        "subtasks": ["a", "b", "c", "d", "e", "f"],
    }
    new_face, _ = await _select(state)
    assert new_face == "worker-deepseek"


# ─── Negative cases ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plain_question_returns_none():
    state = {"task": "What is the capital of France?"}
    new_face, old_face = await _select(state)
    assert new_face is None
    assert old_face is None


@pytest.mark.asyncio
async def test_empty_task_returns_none():
    state = {"task": ""}
    new_face, old_face = await _select(state)
    assert new_face is None
    assert old_face is None


@pytest.mark.asyncio
async def test_planning_verb_without_code_keywords_selects_planner_claude():
    state = {"task": "plan a team offsite"}
    new_face, old_face = await _select(state)
    assert new_face == "planner-minimax"


@pytest.mark.asyncio
async def test_code_keyword_without_planning_returns_none():
    state = {"task": "fix the typo in the README"}
    new_face, old_face = await _select(state)
    assert new_face is None


@pytest.mark.asyncio
async def test_preserves_existing_face_id():
    state = {"task": "plan a refactor", "face_id": "builder-bob"}
    new_face, old_face = await _select(state)
    assert new_face == "planner-kimi"
    assert old_face == "builder-bob"


# ─── route_node integration ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_node_swaps_face_and_backend(monkeypatch):
    from core.nodes import route

    # Prevent any real HTTP discovery
    class FakeRouter:
        async def discover(self):
            return []

    monkeypatch.setattr(route, "_router", FakeRouter())

    state = {
        "task": "design the database schema for users",
        "face_id": "assistant",
        "messages": [],
    }
    result = await route.route_node(state)
    assert result["face_id"] == "planner-kimi"
    assert result["backend"] == "kimi_code"
    system_msgs = [m for m in result.get("messages", []) if m.get("role") == "system"]
    assert any("Face swap" in m.get("content", "") for m in system_msgs)


@pytest.mark.asyncio
async def test_route_node_model_override_preserves_face_escalation_backend(monkeypatch):
    """When model_override is set, escalation_backend must still come from the face."""
    from core.nodes import route
    from core.faces.registry import Face

    fake_face = Face(
        id="worker-kimi",
        name="Worker Kimi",
        system_prompt="x",
        preferred_backend="kimi_code",
        escalation_backend="kimi_platform",
    )

    class FakeRegistry:
        def get_face(self, _): return fake_face

    from core.faces.registry import FaceRegistry as _OrigFaceRegistry
    monkeypatch.setattr(
        "core.faces.registry.get_default_registry",
        lambda: FakeRegistry(),
    )
    monkeypatch.setattr(
        "core.nodes.route.get_default_registry",
        lambda: FakeRegistry(),
        raising=False,
    )

    state = {
        "task": "anything",
        "face_id": "worker-kimi",
        "model_override": "claude_api",
        "messages": [],
    }
    result = await route.route_node(state)
    assert result["backend"] == "claude_api"
    assert result["escalation_backend"] == "kimi_platform"
