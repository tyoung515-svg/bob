"""
BoBClaw Core — Unit tests for the MCP catalog integration.

No real MCP servers are spawned; the MCP client is fully mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from core.nodes.execute import execute_node
from core.permissions import check_tool_access
from core.tools.registry import get_all_tools


# ─── Helpers ──────────────────────────────────────────────────────────────────

class _FakeMcpReadTool:
    name = "mcp__filesystem__read_file"

    def __init__(self) -> None:
        self.calls: list = []

    async def ainvoke(self, args):
        self.calls.append(args)
        return "file contents: hello from mcp"


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


def _tool_call_response(tool_name: str, call_id: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": {"path": "/tmp/demo.txt"}, "id": call_id}],
    )


def _final_response(text: str) -> AIMessage:
    return AIMessage(content=text)


@pytest.fixture
def _patch_event_log(monkeypatch):
    """Prevent the L0 event log from touching SQLite during tests."""
    monkeypatch.setattr(
        "core.nodes.execute._append_agent_turn_event",
        AsyncMock(return_value=None),
    )


# ─── Registry merge ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_all_tools_skips_mcp_load_for_native_only():
    """Faces with only native allowed tools never trigger MCP stdio spawns."""
    with patch(
        "core.tools.registry._get_mcp_tools",
        AsyncMock(side_effect=RuntimeError("MCP must not be loaded")),
    ):
        tools = await get_all_tools(["get_server_time"])

    assert len(tools) == 1
    assert tools[0].name == "get_server_time"


@pytest.mark.asyncio
async def test_get_all_tools_merges_mcp_tools():
    """MCP tools are namespaced and merged when an MCP ID is allowed."""
    fake_tool = _FakeMcpReadTool()
    with patch(
        "core.tools.registry._get_mcp_tools",
        AsyncMock(return_value=[fake_tool]),
    ):
        tools = await get_all_tools(["mcp__filesystem__read_file"])

    names = [t.name for t in tools]
    assert "mcp__filesystem__read_file" in names


# ─── Gating ───────────────────────────────────────────────────────────────────

def test_check_tool_access_allows_mcp_tool_for_mcp_face():
    assert check_tool_access("assistant-tools-mcp", "mcp__filesystem__read_file") is True


def test_check_tool_access_denies_mcp_tool_for_plain_assistant_tools():
    assert check_tool_access("assistant-tools", "mcp__filesystem__read_file") is False


# ─── End-to-end through execute_node ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_node_runs_mcp_read_tool(monkeypatch, _patch_event_log):
    """A tool-enabled face can invoke a mocked MCP read tool via manual dispatch."""
    fake_mcp_tool = _FakeMcpReadTool()
    with patch(
        "core.tools.registry._get_mcp_tools",
        AsyncMock(return_value=[fake_mcp_tool]),
    ):
        final_text = "I read the file."
        fake_model = _FakeModel(
            responses=[
                _tool_call_response("mcp__filesystem__read_file", "call_1"),
                _final_response(final_text),
            ]
        )
        monkeypatch.setattr(
            "core.nodes.execute._build_tool_model", lambda _backend: fake_model
        )

        result = await execute_node(
            {
                "task": "Read /tmp/demo.txt",
                "backend": "deepseek_v4_flash",
                "face_id": "assistant-tools-mcp",
                "messages": [],
                "approval_response": None,
            }
        )

    assert fake_model.bound_tools is not None
    assert any(t.name == "mcp__filesystem__read_file" for t in fake_model.bound_tools)
    assert fake_mcp_tool.calls == [{"path": "/tmp/demo.txt"}]
    assert result.get("error") is None
    assert result["messages"][-1]["content"] == final_text


# ─── Load-time read-only filter (P1.1) ────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_mcp_tools_filters_write_tools(monkeypatch):
    """Only allowlisted read tools enter the catalog; write tools are dropped."""
    from core.mcp.client import _reset_mcp_cache, get_mcp_tools

    _reset_mcp_cache()

    class _ReadTool:
        name = "read_file"

    class _WriteTool:
        name = "write_file"

    fake_client = AsyncMock()
    fake_client.get_tools = AsyncMock(
        return_value=[_ReadTool(), _WriteTool()]
    )
    monkeypatch.setattr(
        "core.mcp.client._build_client",
        lambda: fake_client,
    )

    tools = await get_mcp_tools()
    names = [t.name for t in tools]

    assert "mcp__filesystem__read_file" in names
    assert "write_file" not in names
    assert "mcp__filesystem__write_file" not in names
    assert len(tools) == 1
