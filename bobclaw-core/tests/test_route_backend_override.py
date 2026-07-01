"""
BoBClaw Core — Tests for backend override validation in route_node

Validates that unknown backend overrides are rejected with an error
instead of silently falling through to local execution.
"""
from __future__ import annotations

import pytest

from core.nodes import route


# ── 1. Valid override → returned as backend, face escalation preserved ─────

@pytest.mark.asyncio
async def test_valid_backend_override_routes_to_named_backend(monkeypatch):
    from core.faces.registry import Face

    fake_face = Face(
        id="assistant",
        name="Assistant",
        system_prompt="x",
        preferred_backend="local",
        escalation_backend="gemini_flash",
    )

    class FakeRegistry:
        def get_face(self, _):
            return fake_face

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
        "task": "hi",
        "face_id": "assistant",
        "backend_override": "deepseek_v4_flash",
        "messages": [],
    }
    result = await route.route_node(state)
    assert result["backend"] == "deepseek_v4_flash"
    assert result["escalation_backend"] == "gemini_flash"


@pytest.mark.asyncio
async def test_local_override_is_allowed():
    state = {
        "task": "hi",
        "face_id": "assistant",
        "backend_override": "local",
        "messages": [],
    }
    result = await route.route_node(state)
    assert result["backend"] == "local"


# ── 2. Unknown override → error state ─────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_backend_override_returns_error():
    state = {
        "task": "hi",
        "face_id": "assistant",
        "backend_override": "minimx",
        "messages": [],
    }
    result = await route.route_node(state)
    assert "error" in result
    assert "minimx" in result["error"]
    assert "valid:" in result["error"]


@pytest.mark.asyncio
async def test_unknown_backend_override_includes_valid_list():
    state = {
        "task": "hi",
        "face_id": "assistant",
        "backend_override": "nonexistent_backend_xyz",
        "messages": [],
    }
    result = await route.route_node(state)
    assert result["error"]
    assert "nonexistent_backend_xyz" in result["error"]
    assert "valid:" in result["error"]
    assert "deepseek_v4_flash" in result["error"]
    assert "local" in result["error"]


# ── 4. Empty/None override → normal face-resolution unchanged ──────────────

@pytest.mark.asyncio
async def test_none_override_skips_validation(monkeypatch):
    route_calls = []

    original = route.route_node

    async def _tracking(state):
        route_calls.append(state.get("backend_override"))
        return await original(state)

    monkeypatch.setattr(route, "route_node", _tracking)

    state = {
        "task": "hello world",
        "face_id": "assistant",
        "backend_override": None,
        "messages": [],
    }
    result = await _tracking(state)
    assert route_calls[0] is None
    assert "error" not in result
    assert "backend" in result


@pytest.mark.asyncio
async def test_empty_string_override_skips_validation(monkeypatch):
    route_calls = []

    original = route.route_node

    async def _tracking(state):
        route_calls.append(state.get("backend_override"))
        return await original(state)

    monkeypatch.setattr(route, "route_node", _tracking)

    state = {
        "task": "hello world",
        "face_id": "assistant",
        "backend_override": "",
        "messages": [],
    }
    result = await _tracking(state)
    assert route_calls[0] == ""
    assert "error" not in result
    assert "backend" in result


@pytest.mark.asyncio
async def test_missing_override_key_skips_validation():
    state = {
        "task": "hello world",
        "face_id": "assistant",
        "messages": [],
    }
    result = await route.route_node(state)
    assert "error" not in result
    assert "backend" in result
