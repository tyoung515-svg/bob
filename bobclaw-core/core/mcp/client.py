"""
BoBClaw Core — MCP client

Loads tools from configured stdio MCP servers. Tools are loaded once per
process; each worker process manages its own stdio subprocesses.

P1 is read-only: only filesystem read tools are allowlisted by faces.
Write/delete tools are never exposed.
"""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from core.config import config

logger = logging.getLogger(__name__)

# Default-deny allowlist of read-only filesystem tools (checked against the
# original tool name before namespacing). Future non-filesystem servers can
# extend this set or move to per-server config.
_READONLY_MCP_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "read_text_file",
    "read_media_file",
    "read_multiple_files",
    "list_directory",
    "list_directory_with_sizes",
    "directory_tree",
    "get_file_info",
    "search_files",
    "list_allowed_directories",
})

# Process-local cache. Each worker process gets its own stdio children.
_MCP_CLIENT: MultiServerMCPClient | None = None
_MCP_TOOLS: list[BaseTool] | None = None


def _build_client() -> MultiServerMCPClient:
    servers = config.MCP_SERVERS or {}
    if not servers:
        logger.debug("No MCP servers configured")
    return MultiServerMCPClient(servers)


async def get_mcp_tools() -> list[BaseTool]:
    """Return the namespaced tools from all configured MCP servers.

    Loads once per process and caches the result. Tool names are prefixed as
    ``mcp__<server_name>__<tool_name>`` so they never collide with native tools.
    """
    global _MCP_CLIENT, _MCP_TOOLS
    if _MCP_TOOLS is not None:
        return list(_MCP_TOOLS)

    client = _build_client()
    namespaced: list[BaseTool] = []
    try:
        for server_name in config.MCP_SERVERS or {}:
            try:
                tools = await client.get_tools(server_name=server_name)
            except Exception as exc:
                logger.warning(
                    "Failed to load MCP tools from server %r: %s", server_name, exc
                )
                continue
            for tool in tools:
                if tool.name not in _READONLY_MCP_TOOLS:
                    continue  # default-deny: drop write/edit/move/create + anything unknown
                tool.name = f"mcp__{server_name}__{tool.name}"
                namespaced.append(tool)
    except Exception as exc:
        logger.warning("Failed to load MCP tools: %s", exc)

    _MCP_CLIENT = client
    _MCP_TOOLS = namespaced
    return list(namespaced)


def _reset_mcp_cache() -> None:
    """Clear the process-local cache (test hook)."""
    global _MCP_CLIENT, _MCP_TOOLS
    _MCP_CLIENT = None
    _MCP_TOOLS = None
