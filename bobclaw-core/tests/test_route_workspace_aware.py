"""
BoBClaw Core — Unit tests for workspace-aware routing and OpenCode fallback
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.nodes import execute as execute_module
from core.nodes.route import route_node


# ─── route.py workspace validation ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_opencode_without_workspace_falls_back():
    state = {
        "task": "fix auth",
        "face_id": "worker-opencode",
        "messages": [],
        "workspace_dir": None,
    }
    with patch.object(execute_module._router, "discover", new_callable=AsyncMock):
        result = await route_node(state)
    assert result["backend"] == "kimi_platform"
    assert any(
        "requires workspace_dir" in m.get("content", "")
        for m in result.get("messages", [])
    )


@pytest.mark.asyncio
async def test_worker_opencode_with_workspace_routes_to_opencode():
    state = {
        "task": "fix auth",
        "face_id": "worker-opencode",
        "messages": [],
        "workspace_dir": "/tmp/ws",
    }
    with patch.object(execute_module._router, "discover", new_callable=AsyncMock):
        result = await route_node(state)
    assert result["backend"] == "opencode_serve"


# ─── execute_node NoOpenCodeAvailable fallback ────────────────────────────────

@pytest.mark.asyncio
async def test_execute_node_fallback_on_no_opencode_available(mock_redis):
    async def _mock_dispatch(messages, workspace_dir):
        from core.backends.opencode_pool import NoOpenCodeAvailable
        raise NoOpenCodeAvailable("no instance")

    async def _mock_send(messages, backend, model_override=None):
        return f"from {backend}"

    with patch.object(
        execute_module, "_send_to_backend", side_effect=_mock_send
    ):
        with patch(
            "core.backends.opencode_pool._pool.dispatch",
            side_effect=_mock_dispatch,
        ):
            result = await execute_module.execute_node(
                {
                    "task": "do something",
                    "backend": "opencode_serve",
                    "messages": [],
                    "workspace_dir": "/tmp/ws",
                    "escalation_backend": "kimi_platform",
                }
            )

    assert result["messages"][0]["content"] == "from kimi_platform"


@pytest.mark.asyncio
async def test_execute_node_opencode_success_no_fallback(mock_redis):
    async def _mock_dispatch(messages, workspace_dir):
        return "opencode response"

    with patch(
        "core.backends.opencode_pool._pool.dispatch",
        side_effect=_mock_dispatch,
    ):
        result = await execute_module.execute_node(
            {
                "task": "do something",
                "backend": "opencode_serve",
                "messages": [],
                "workspace_dir": "/tmp/ws",
                "escalation_backend": "kimi_platform",
            }
        )

    assert result["messages"][0]["content"] == "opencode response"
