"""
BoBClaw Core — Unit tests for the create_project native tool.

All database I/O is mocked; no live Postgres calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from core.nodes.execute import execute_node
from core.tools.projects import (
    _current_conversation_id,
    _current_user_id,
    create_project,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_async_cm(return_value):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=return_value)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _fake_pool(row=None):
    """Return a mock asyncpg pool + connection that records calls."""
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=row or {"id": "proj-uuid", "name": "Demo Project"})
    conn.execute = AsyncMock(return_value="UPDATE 1")
    conn.transaction = MagicMock(side_effect=lambda: _make_async_cm(None))
    pool = AsyncMock()
    pool.acquire = MagicMock(side_effect=lambda: _make_async_cm(conn))
    return pool, conn


@pytest.fixture
def _patch_event_log(monkeypatch):
    monkeypatch.setattr(
        "core.nodes.execute._append_agent_turn_event",
        AsyncMock(return_value=None),
    )


# ─── Direct tool tests ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_project_inserts_row_and_assigns_conversation(monkeypatch):
    pool, conn = _fake_pool()
    monkeypatch.setattr("core.tools.projects.get_pool", lambda: pool)

    user_token = _current_user_id.set("user-123")
    conv_token = _current_conversation_id.set("conv-456")
    try:
        result = await create_project.ainvoke(
            {
                "name": "  Demo Project  ",
                "description": "A test project",
                "instructions": "Be helpful",
                "default_face": "assistant",
                "default_backend": "deepseek_v4_flash",
            }
        )
    finally:
        _current_user_id.reset(user_token)
        _current_conversation_id.reset(conv_token)

    assert "Created project 'Demo Project'" in result
    assert "proj-uuid" in result

    call = conn.fetchrow.call_args
    assert call.args[1] == "user-123"
    assert call.args[2] == "Demo Project"
    assert call.args[3] == "A test project"
    assert call.args[4] == "Be helpful"
    assert call.args[5] == "assistant"
    assert call.args[6] == "deepseek_v4_flash"

    update_call = conn.execute.call_args
    assert update_call.args[1] == "proj-uuid"
    assert update_call.args[2] == "conv-456"
    assert update_call.args[3] == "user-123"


@pytest.mark.asyncio
async def test_create_project_fail_closed_without_user_id(monkeypatch):
    pool, conn = _fake_pool()
    monkeypatch.setattr("core.tools.projects.get_pool", lambda: pool)

    # Ensure no lingering contextvar value from another test.
    user_token = _current_user_id.set(None)
    try:
        result = await create_project.ainvoke({"name": "Orphan"})
    finally:
        _current_user_id.reset(user_token)

    assert "user_id is required" in result
    conn.fetchrow.assert_not_called()


# ─── End-to-end through execute_node ──────────────────────────────────────────

class _FakeBoundModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.invocations: list = []

    async def ainvoke(self, messages):
        self.invocations.append(messages)
        if len(self.invocations) <= len(self.responses):
            return self.responses[len(self.invocations) - 1]
        return self.responses[-1]


class _FakeModel:
    def __init__(self, responses):
        self._responses = responses
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return _FakeBoundModel(self._responses)


def _tool_call_response(tool_name: str, args: dict, call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": call_id}])


@pytest.mark.asyncio
async def test_execute_node_threads_identity_to_create_project(
    monkeypatch, _patch_event_log
):
    """A create_project tool_call receives user_id/conversation_id via contextvars."""
    pool, conn = _fake_pool()
    monkeypatch.setattr("core.tools.projects.get_pool", lambda: pool)

    final_text = "Project created successfully."
    fake_model = _FakeModel(
        responses=[
            _tool_call_response("create_project", {"name": "E2E Demo"}, "call_1"),
            AIMessage(content=final_text),
        ]
    )
    monkeypatch.setattr(
        "core.nodes.execute._build_tool_model", lambda _backend: fake_model
    )

    result = await execute_node(
        {
            "task": "Make a project called E2E Demo",
            "backend": "deepseek_v4_flash",
            "face_id": "assistant-actions",
            "user_id": "user-789",
            "conversation_id": "conv-abc",
            "messages": [],
            "approval_response": None,
        }
    )

    assert result.get("error") is None
    assert result["messages"][-1]["content"] == final_text
    assert fake_model.bound_tools is not None
    assert any(t.name == "create_project" for t in fake_model.bound_tools)

    call = conn.fetchrow.call_args
    assert call.args[1] == "user-789"
    assert call.args[2] == "E2E Demo"

    update_call = conn.execute.call_args
    assert update_call.args[2] == "conv-abc"
    assert update_call.args[3] == "user-789"
