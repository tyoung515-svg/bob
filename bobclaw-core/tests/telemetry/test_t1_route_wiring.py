"""T1 flight-aware routing (Lane 1b) — route_node fast-path wiring (gated, byte-identical).

Proves the fast path is INERT by default (byte-identical routing), fires only when enabled
AND a servable profile wins, and never touches a pin_authoritative turn.
"""
from __future__ import annotations

import pytest

from core.config import config
from core.mcp_tools.profile_routing import namespaced_key
from core.mcp_tools.profiles import ProfileStatus, seed_probationary
from core.mcp_tools.reliability import RedisProfileCache
from core.nodes import route


class _FakeRouter:
    async def discover(self):
        return []


# ── _t1_fastpath_face gating ──────────────────────────────────────────────────
async def test_fastpath_disabled_by_default(monkeypatch):
    # Default config.T1_FASTPATH_ENABLED is False → None even with a servable profile.
    cache = RedisProfileCache(redis_getter=None)
    p = seed_probationary("k", [("t", "h")], "worker")
    p.status = ProfileStatus.CRYSTALLIZED
    await cache.put(namespaced_key("chat", "default"), p)
    monkeypatch.setattr(config, "T1_FASTPATH_ENABLED", False)
    face = await route._t1_fastpath_face({"task": "hi", "face_id": "assistant"}, cache=cache)
    assert face is None


async def test_fastpath_enabled_hit(monkeypatch):
    cache = RedisProfileCache(redis_getter=None)
    p = seed_probationary("k", [("t", "h")], "worker")
    p.status = ProfileStatus.CRYSTALLIZED
    await cache.put(namespaced_key("chat", "default"), p)
    monkeypatch.setattr(config, "T1_FASTPATH_ENABLED", True)
    monkeypatch.setattr(config, "T1_FASTPATH_EPSILON", 0.0)  # never explore
    face = await route._t1_fastpath_face({"task": "hi", "face_id": "assistant"}, cache=cache)
    assert face == "worker-deepseek"


# ── route_node integration ────────────────────────────────────────────────────
async def test_route_node_uses_fastpath_face(monkeypatch):
    monkeypatch.setattr(route, "_router", _FakeRouter())

    async def _fake_fp(state, cache=None):
        return "worker-deepseek"

    monkeypatch.setattr(route, "_t1_fastpath_face", _fake_fp)
    result = await route.route_node({"task": "anything", "face_id": "assistant", "messages": []})
    assert result["face_id"] == "worker-deepseek"
    assert any("Face swap" in m.get("content", "") for m in result.get("messages", []))


async def test_route_node_fastpath_none_falls_back_to_heuristic(monkeypatch):
    monkeypatch.setattr(route, "_router", _FakeRouter())

    async def _none_fp(state, cache=None):
        return None

    monkeypatch.setattr(route, "_t1_fastpath_face", _none_fp)
    # a code-shaped planning task → _select_face heuristic yields planner-kimi (byte-identical)
    result = await route.route_node(
        {"task": "plan a refactor of the auth module", "face_id": "assistant", "messages": []}
    )
    assert result["face_id"] == "planner-kimi"


async def test_route_node_pin_authoritative_never_calls_fastpath(monkeypatch):
    monkeypatch.setattr(route, "_router", _FakeRouter())
    called = {"n": 0}

    async def _sentinel_fp(state, cache=None):
        called["n"] += 1
        return "worker-deepseek"

    monkeypatch.setattr(route, "_t1_fastpath_face", _sentinel_fp)
    result = await route.route_node({
        "task": "plan a refactor", "face_id": "planner-cc-edit",
        "pin_authoritative": True, "messages": [],
    })
    # pin authority stays ABOVE the fast path — it must not be consulted, face is honored.
    assert called["n"] == 0
    assert result["face_id"] == "planner-cc-edit"
