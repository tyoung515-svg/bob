"""
BoBClaw Core — Native + MCP tool registry.

P0 registers a single trivial, deterministic native tool to prove the
LangChain tool-calling loop end-to-end. P1 merges in read-only MCP tools,
gated by the same face-level ``allowed_tools`` list.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from langchain_core.tools import BaseTool, tool

from core.mcp.client import get_mcp_tools as _get_mcp_tools
from core.tools.projects import create_project
from core.tools.teams_tool import create_team, list_backends


@tool
def get_server_time() -> str:
    """Return the current server time in ISO 8601 format (UTC)."""
    return datetime.now(timezone.utc).isoformat()


# Tool-id → LangChain tool instance. New native tools register here.
NATIVE_TOOLS: dict[str, Callable] = {
    "get_server_time": get_server_time,
    "create_project": create_project,
    "list_backends": list_backends,
    "create_team": create_team,
}


def get_tools(allowed: list[str]) -> list[Callable]:
    """Return the subset of native tools permitted by *allowed* tool IDs.

    Unknown IDs are silently ignored — they may belong to MCP servers (P1)
    or future native tools that are not loaded in this process.
    """
    return [t for name, t in NATIVE_TOOLS.items() if name in allowed]


async def get_all_tools(allowed: list[str]) -> list[BaseTool]:
    """Return native tools plus MCP tools permitted by *allowed* tool IDs.

    MCP tools are only loaded when *allowed* contains at least one ID that is
    not a native tool, so faces without MCP access never pay the stdio spawn
    cost and remain fully testable without MCP dependencies.
    """
    native = get_tools(allowed)
    non_native_allowed = [name for name in allowed if name not in NATIVE_TOOLS]
    if not non_native_allowed:
        return list(native)

    try:
        mcp_tools = await _get_mcp_tools()
    except Exception:
        # MCP is additive; if the server is unreachable/misconfigured, fall
        # back to native tools rather than crashing the turn.
        mcp_tools = []

    allowed_set = set(non_native_allowed)
    permitted_mcp = [t for t in mcp_tools if t.name in allowed_set]
    return list(native) + list(permitted_mcp)
