"""
BoBClaw Core — Native + MCP tool registry.

P0 registers a single trivial, deterministic native tool to prove the
LangChain tool-calling loop end-to-end. P1 merges in read-only MCP tools,
gated by the same face-level ``allowed_tools`` list.
"""
from __future__ import annotations

import contextvars
import json

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr

from datetime import datetime, timezone
from typing import Callable, Literal

from langchain_core.tools import BaseTool, tool

from core.mcp.client import get_mcp_tools as _get_mcp_tools
from core.tools.conv_tools import conv_expand, conv_search, conversations_find, conversations_read
from core.tools.memory_tools import memory_recall, memory_save, memory_retire, memory_correct
from core.tools.projects import create_project
from core.tools.teams_tool import create_team, list_backends
from core.tools.work import work_catalog, work_recipe_save, work_prepare, work_start, work_status, work_cancel, work_team_save, work_team_start, work_run_status, work_run_cancel
from core.tools.ask import ask_operator
from core.tools.builder_tools import BUILDER_TOOLS
from core.tools.pc_tools import PC_TOOLS
from core.tools.web import web_fetch, web_search
from core.tools.workflows import WORKFLOW_TOOLS
from core.tools.inspection import INSPECTION_TOOLS


@tool
def get_server_time() -> str:
    """Return the current UTC ISO 8601 timestamp followed by its weekday name."""
    now = datetime.now(timezone.utc)
    weekday = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')[now.weekday()]
    return f"{now.isoformat()} ({weekday})"


_skill_face_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "skill_face_id", default=None
)


class SkillListArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillViewArgs(SkillListArgs):
    skill_id: str
    version: StrictInt | None = None


def _skill_registry():
    from core.faces.registry import get_default_registry
    face_id = _skill_face_id.get()
    if not face_id:
        raise PermissionError("missing skill invocation face")
    return get_default_registry(), face_id


@tool(args_schema=SkillListArgs)
def skill_list() -> str:
    """Discover allowed skill summaries. Bodies require skill_view."""
    registry, face_id = _skill_registry()
    from core.execution.workflow_context import current
    workflow = current.get()
    return json.dumps(workflow.skill_summaries(registry) if workflow else registry.list_skills(face_id))


@tool(args_schema=SkillViewArgs)
def skill_view(skill_id: str, version: int | None = None) -> str:
    """Load an allowed procedure by skill ID and optional version."""
    registry, face_id = _skill_registry()
    from core.execution.workflow_context import current
    workflow = current.get()
    if workflow and not workflow.skill_allowed(registry,skill_id):
        from core.execution.agent_turn import ToolRefusal
        raise ToolRefusal('skill_unavailable',403)
    return json.dumps(registry.load_skill(face_id, skill_id, version))


class GuideArgs(SkillListArgs):
    topic: str = "index"


@tool(args_schema=GuideArgs)
async def bob_guide(topic: str = "index") -> dict:
    """Read Bob's scoped product guide: index or one topic. No model call or memory write."""
    from core.guide import render
    registry, face_id = _skill_registry()
    return await render(topic, registry=registry, face_id=face_id)


PIPE_OPERATIONS = frozenset({"runs.get"})


class RunGetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: StrictStr


class PipeCallArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["runs.get"]
    args: RunGetArgs


@tool(args_schema=PipeCallArgs)
async def pipe_call(op: str, args: RunGetArgs) -> dict:
    """Read an owned run via runs.get with args {run_id}; no other operation exists."""
    import hashlib
    import time
    import uuid
    import aiohttp
    from core.config import config
    from core.permissions import canonical_pipe_json, compute_pipe_vouch
    from core.tools.projects import _current_user_id, _current_conversation_id

    operation_id = uuid.uuid4().hex
    result = dict(ok=False, operation_id=operation_id, status="refused",
                  receipt=dict(kind="run", id=None, independently_verified=False),
                  warnings=[])
    user, conversation, face = (_current_user_id.get(),
                               _current_conversation_id.get(), _skill_face_id.get())
    if not all(isinstance(v, str) and v.strip() for v in (user, conversation, face)):
        result["warnings"] = ["missing_identity"]
        return result
    if op not in PIPE_OPERATIONS:
        result["warnings"] = ["unknown_operation"]
        return result
    arguments = RunGetArgs.model_validate(args).model_dump()
    now = int(time.time())
    claims = dict(iss="core", aud="gateway", sub=user, face=face,
                  conversation_id=conversation, op=op,
                  args_sha256=hashlib.sha256(canonical_pipe_json(arguments).encode()).hexdigest(),
                  iat=now, exp=now + 60, nonce=operation_id)
    vouch = compute_pipe_vouch(claims, config.BOBCLAW_SECRET)
    if not vouch:
        result["warnings"] = ["bad_envelope"]
        return result
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.post(config.GATEWAY_URL.rstrip("/") + "/chat/pipes",
                                    json=dict(claims=claims, vouch=vouch, args=arguments)) as response:
                payload = await response.json()
                if response.status == 200:
                    return payload
                result["warnings"] = [payload.get("error", {}).get("code", "bad_envelope")]
    except (TimeoutError, aiohttp.ClientError, ValueError):
        result.update(status="unknown", warnings=["transport_failure"])
    return result


# Tool-id → LangChain tool instance. New native tools register here.
NATIVE_TOOLS: dict[str, Callable] = {
    **{t.name: t for t in INSPECTION_TOOLS},
    **{t.name: t for t in WORKFLOW_TOOLS},
    **{t.name: t for t in PC_TOOLS},
    **{t.name: t for t in BUILDER_TOOLS},
    "work_catalog": work_catalog,
    "work_recipe_save": work_recipe_save,
    "work_prepare": work_prepare,
    "work_start": work_start,
    "work_status": work_status,
    "work_cancel": work_cancel,
    "work_team_save": work_team_save, "work_team_start": work_team_start,
    "work_run_status": work_run_status, "work_run_cancel": work_run_cancel,
    "pipe_call": pipe_call,
    "bob_guide": bob_guide,
    "skill_list": skill_list,
    "skill_view": skill_view,
    "get_server_time": get_server_time,
    "create_project": create_project,
    "list_backends": list_backends,
    "create_team": create_team,
    "memory_save": memory_save,
    "memory_recall": memory_recall,
    "memory_retire": memory_retire,
    "memory_correct": memory_correct,
    "conv_expand": conv_expand,
    "conv_search": conv_search,
    "conversations_find": conversations_find,
    "conversations_read": conversations_read,
    "web_fetch": web_fetch,
    "web_search": web_search,
    "ask_operator": ask_operator,
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
