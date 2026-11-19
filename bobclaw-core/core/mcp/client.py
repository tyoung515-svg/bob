"""MCP uses configured legacy clients or the packaged lane worker's stdio servers."""
from __future__ import annotations

from typing import Any
from langchain_mcp_adapters.client import MultiServerMCPClient
from core.config import config
from core.worker import mode as execution_mode
from core.worker import client as worker_client

import json
import logging
from langchain_core.tools import BaseTool, StructuredTool
from core.worker.client import execute_async, WorkerUnavailable

logger = logging.getLogger(__name__)
_MCP_CLIENT: MultiServerMCPClient | None = None
_MCP_TOOLS: list[BaseTool] | None = None
_READONLY_MCP_TOOLS = frozenset({'read_file', 'read_text_file', 'read_media_file',
    'read_multiple_files', 'list_directory', 'list_directory_with_sizes', 'directory_tree',
    'get_file_info', 'search_files', 'list_allowed_directories'})

async def _rpc(payload):
    result = await execute_async('mcp', input=json.dumps(payload), timeout=120)
    if result['returncode'] or result.get('killed'):
        raise WorkerUnavailable('unavailable: worker MCP server failed')
    return json.loads(result['stdout'])

async def get_mcp_tools() -> list[BaseTool]:
    """Return the namespaced tools from all configured MCP servers.

    Loads once per process and caches the result. Tool names are prefixed as
    ``mcp__<server_name>__<tool_name>`` so they never collide with native tools.
    """
    global _MCP_CLIENT, _MCP_TOOLS
    if _MCP_TOOLS is not None:
        return list(_MCP_TOOLS)
    if execution_mode() == "legacy":
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
    else:
        tools = []
        # Only installed/root-enrolled servers. MCP_SERVERS cannot inject a command.
        for server in ('filesystem', 'browser'):
            try:
                rows = await _rpc({'operation': 'list', 'server': server})
                if server == 'browser':
                    async def browser_sequence(calls):
                        return await _rpc({'operation': 'sequence', 'server': 'browser', 'calls': calls})
                    tools.append(StructuredTool.from_function(
                        coroutine=browser_sequence, name='mcp__browser__sequence',
                        description='Run up to 16 browser tool calls in one disposable browser session. '
                                    'Available calls: ' + ', '.join(row['name'] for row in rows),
                        args_schema={'type': 'object', 'properties': {'calls': {'type': 'array',
                            'minItems': 1, 'maxItems': 16, 'items': {'type': 'object',
                            'properties': {'name': {'type': 'string'}, 'arguments': {'type': 'object'}},
                            'required': ['name'], 'additionalProperties': False}}},
                            'required': ['calls'], 'additionalProperties': False}))
                    continue
                for row in rows:
                    if server == 'filesystem' and row['name'] not in _READONLY_MCP_TOOLS:
                        continue
                    def bind(server, name):
                        async def call(**kwargs):
                            return await _rpc({'operation': 'call', 'server': server,
                                               'name': name, 'arguments': kwargs})
                        return call
                    tools.append(StructuredTool.from_function(
                        coroutine=bind(server, row['name']), name=f'mcp__{server}__{row["name"]}',
                        description=row.get('description') or row['name'], args_schema=row['inputSchema']))
            except (OSError, RuntimeError, ValueError):
                logger.warning('MCP server %s unavailable through worker boundary', server)
        if tools:
            _MCP_TOOLS = tools
        return tools

def _reset_mcp_cache() -> None:
    """Clear the process-local cache (test hook)."""
    global _MCP_CLIENT, _MCP_TOOLS
    if execution_mode() == "legacy":
        _MCP_CLIENT = None
    else:
        pass
    _MCP_TOOLS = None


def _build_client() -> MultiServerMCPClient:
    if execution_mode() == "legacy":
        servers = config.MCP_SERVERS or {}
        if not servers:
            logger.debug("No MCP servers configured")
        return MultiServerMCPClient(servers)
    else:
        raise worker_client.WorkerUnavailable("unavailable: legacy execution helper")
