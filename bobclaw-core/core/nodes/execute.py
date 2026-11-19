"""
BoBClaw Core — Generic execution node

Sends the conversation to the chosen backend, accumulates the streamed
response into state.messages, detects dangerous actions, and flags
state.approval_required when human sign-off is needed.

Per-node streaming is driven by LangGraph's ``stream_mode="updates"``
surfaced by ``/api/chat``; that way we get one delta per node boundary
without needing a ``StreamWriter`` (which requires Python 3.11+ contextvar
propagation).  Per-token streaming via ``StreamWriter`` is a follow-up
tracked in SPRINT_PLAN.md once we drop Python 3.10 support.
"""
from __future__ import annotations

import asyncio
import json
import uuid
import logging
from time import perf_counter
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Optional

import aiohttp
import redis.asyncio as aioredis

from core.backends._cost import check_cap, parse_usage, track_cost, usd_for
from core.backends._lc_openai import TOOL_CAPABLE_BACKENDS, build_chat_openai, tool_backend
from core.backends.local_router import LocalModelRouter
from core.config import config
from core.roster import get_roster
from core.lane_config import configured_mode, get_lane, require_backend, require_state, LaneConfigurationError
from core.nodes._l0_events import _append_agent_turn_event
from core.permissions import check_tool_access, requires_approval, task_requires_approval
from core.telemetry.spend import bind_flight_from_state, record_current_flight_spend
from core.tools.projects import _current_conversation_id, _current_user_id
from core.tools.memory_tools import (
    memory_tool_conversation_id,
    memory_tool_face_id,
    memory_tool_user_id,
)
from core.tools.registry import NATIVE_TOOLS, get_all_tools

if TYPE_CHECKING:
    from core.graph import AgentState

logger = logging.getLogger(__name__)
_execute_fallbacks = get_roster().defaults.execute_fallbacks

# ── Locale response directive (i18n S0) ───────────────────────────────────────
# The ONE place the response-language directive text lives. Module-level so tests can
# import and assert the exact bytes (guards drift). Injected before other context
# in execute_node for known non-"en" locales; gated face identity/card precede it.
LOCALE_DIRECTIVE = {
    "zh-Hans": "只用简体中文回答。无论用户消息或上下文使用何种语言，你的全部回复都必须是简体中文。",
    "zh-Hant": "只用繁體中文回答。無論使用者訊息或上下文使用何種語言，你的全部回覆都必須是繁體中文。",
}

def locale_directive_message(locale) -> Optional[dict]:
    """The locale directive ahead of existing context, or None to inject nothing.
    None / "en" / unknown / non-string => None (byte-identical English path). The SINGLE
    source of the locale-injection predicate; execute_node and the tests both call this."""
    if isinstance(locale, str) and locale != "en" and locale in LOCALE_DIRECTIVE:
        return {"role": "system", "content": LOCALE_DIRECTIVE[locale]}
    return None


# ── Ask-Bob helper bubble page context (MS9 U5, SPEC §3 / D3) ──────────────────
# The header line the page-context system card leads with. Captured as a constant so a
# test can assert exact bytes (guards drift), mirroring LOCALE_DIRECTIVE.
PAGE_CONTEXT_HEADER = (
    "The user is viewing a screen in the BoBClaw app (not this chat) and is asking the "
    "Ask-Bob helper about it. Use the following snapshot to answer questions about what "
    "is on their screen; do not invent state that is not shown."
)


def page_context_card(page_context, *, enabled: bool) -> Optional[dict]:
    """The front-adjacent system card for the helper bubble's page snapshot, or None.

    The SINGLE source of the page-context injection predicate (execute_node and the U5
    tests both call this — mirrors ``locale_directive_message``). Returns None — injecting
    NOTHING — whenever:

    * ``enabled`` is False (``config.PAGE_CONTEXT_ENABLED`` off) — the HARD flag gate: the
      assembled prompt is byte-identical regardless of what the client sent (U5 accept #1); or
    * ``page_context`` is not a non-empty dict (absent / None / wrong type / ``{}``).

    Otherwise returns ``{"role": "system", "content": <deterministic card>}``: the header,
    then ``Screen: <page>`` when a string ``page`` is present, then ``Visible state:\\n<snap>``
    where ``snapshot`` is used verbatim if a string, else JSON with ``sort_keys=True`` (a
    stable rendering so the same snapshot always yields the same bytes).
    """
    if not enabled:
        return None
    if not isinstance(page_context, dict) or not page_context:
        return None
    lines = [PAGE_CONTEXT_HEADER]
    page = page_context.get("page")
    if isinstance(page, str) and page.strip():
        lines.append(f"Screen: {page.strip()}")
    snapshot = page_context.get("snapshot")
    if snapshot is not None:
        snap_text = (
            snapshot.strip()
            if isinstance(snapshot, str)
            else json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        )
        if snap_text:
            lines.append(f"Visible state:\n{snap_text}")
    return {"role": "system", "content": "\n".join(lines)}


# ── LangChain / LangGraph imports for the opt-in tool loop (P0) ────────────────
# Imported locally so the rest of execute.py keeps running even if these
# packages are not available; P0 adds langchain-openai as a required dep.
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage


# ── CC approved-edit path (C4) ───────────────────────────────────────────────

# Captured at import time so a test patching the ClaudeCodeClient class symbol
# can't subvert the scratch-write guard (it stays the genuine staticmethod).
from core.backends.claude_code import ClaudeCodeClient as _CCClient
_cc_is_scratch_write = _CCClient._is_scratch_write


def _clear_scratch_diffs(scratch_dir: Optional[str]) -> None:
    """Delete every ``proposed_*.diff`` in *scratch_dir* (F3).

    Once captured into the approval item, the on-disk diffs are spent. Leaving them lets
    the NEXT turn — which may propose nothing — re-capture and re-apply the prior turn's
    edit (the planner-cc-edit scratch dir persists per conversation). Best-effort: a
    missing dir or an unlink error is swallowed (the capture already succeeded)."""
    if not scratch_dir:
        return
    import glob as _glob
    import os as _os

    try:
        for path in _glob.glob(_os.path.join(scratch_dir, "proposed_*.diff")):
            try:
                _os.remove(path)
            except OSError:
                pass
    except OSError:
        pass


async def _maybe_capture_cc_edit(
    client,
    posture: dict,
    response: str,
    conversation_id: str,
    session_id: Optional[str],
    state_scope: Optional[dict] = None,
    user_id: Optional[str] = None,
) -> Optional[dict]:
    """Capture a proposed CC edit and, if found, raise a ``cc_edit`` approval.

    Only fires for the scratch-write posture (the planner-cc-edit face). Reads
    ``proposed_*.diff`` from the SAME scratch dir the client used for the spawn
    (``client._scratch_dir()`` — reuses the path/sanitisation logic, no
    duplication), falling back to an inline ```diff block in *response*.

    *state_scope* is the per-turn scope from ``AgentState`` (Neck Beard P3: the agent
    TOKEN's vouched blast radius). It takes PRECEDENCE over the static face posture's
    scope so a headless agent's destructive cc_edit is gated by the credential it holds,
    not by whatever the face profile happened to declare. ``None`` ⇒ fall back to the
    face posture (today's behaviour, byte-identical for the non-agent path).

    Returns a state-delta dict that parks the diff (``cc_pending_edit``), flags
    ``approval_required``, and surfaces the approval ``action_type``/``details``
    for the SSE layer — or ``None`` when there is no diff (normal planner turn)
    or the routing seam does not send it to a human.
    """
    from core.nodes.cc_edit import capture_cc_edit, route_approval

    # Use the genuine scratch-write check captured at import time (immune to a
    # test patching the ClaudeCodeClient class symbol). A non-scratch posture
    # (plain permission_mode: plan) can never propose an edit here.
    if not _cc_is_scratch_write(posture):
        return None

    # The proposed-diff dir derivation lives on the client (same path/sanitisation
    # as the spawn). claude_code exposes ``_scratch_dir()``; codex_code exposes
    # ``_work_dir()`` — both are the per-conversation dir where the CLI wrote
    # ``proposed_<n>.diff``. Reuse whichever the client provides (no duplication);
    # a fake test client lacking both simply skips capture.
    scratch_fn = getattr(client, "_scratch_dir", None) or getattr(
        client, "_work_dir", None
    )
    if scratch_fn is None:
        return None
    scratch_dir = scratch_fn()
    diffs = capture_cc_edit(scratch_dir, response)
    if not diffs:
        return None
    # F3: the proposed diffs are now captured into the approval item — consume the
    # on-disk source so a later turn that proposes NOTHING new can't re-capture (and
    # re-apply) this turn's edit. The parked/auto diff lives in state, not the file.
    _clear_scratch_diffs(scratch_dir)

    primary = diffs[0]
    combined_diff = "\n".join(d["unified_diff"] for d in diffs)
    file_paths = [d["file_path"] for d in diffs if d.get("file_path")]
    details = {
        "file_path": primary.get("file_path"),
        "file_paths": file_paths,
        "diff": combined_diff,
        "conversation_id": conversation_id or None,
        "session_id": session_id,
        "summary": (response or "").strip()[:280],
        # Carry the job's scope so the Gate router can decide auto-clear vs escalate
        # (INTAKE.md scope: block). P3 — the agent TOKEN's vouched scope (state_scope)
        # wins over the static face posture; fall back to the posture when no token
        # scope rode in. A present-but-empty token scope ({}) is honored (all-gate),
        # so the test is `is not None`, not truthiness.
        "scope": state_scope if state_scope is not None else (posture.get("scope") or {}),
        # Critic backend for the Gate's middle tier (P2). Optional; without it
        # ambiguous actions fail closed to human.
        "critic_backend": posture.get("critic_backend"),
    }

    if not requires_approval("cc_edit"):
        return None
    destination = await route_approval("cc_edit", details)
    # Defense-in-depth: only honor an AUTO-clear when EVERY captured diff has a
    # scope-checkable path. A hunk whose target path the parser couldn't identify is
    # excluded from the Gate's path check (details["file_paths"]) yet IS in the applied
    # combined diff — so an unidentifiable path must never ride an auto-clear. Such a
    # diff falls through to the human queue (fail closed).
    all_paths_known = bool(diffs) and all(d.get("file_path") for d in diffs)
    if destination == "auto" and all_paths_known:
        # Gate auto-cleared an IN-SCOPE edit (P3 — cc_edit is gateable now). Skip the
        # human interrupt, write the gate-audit row (approved_by='gate'), and apply the
        # diff core-direct — still double-gated by CC_EDIT_APPLY_ENABLED (default off).
        return await _auto_apply_gate_cc_edit(
            combined_diff, file_paths, details, conversation_id, user_id
        )

    return {
        "cc_pending_edit": {"diff": combined_diff, "file_paths": file_paths},
        "approval_required": True,
        "approval_response": None,
        "approval_action_type": "cc_edit",
        "approval_details": details,
    }


async def _audit_cc_edit_gate(
    user_id: Optional[str],
    conversation_id: Optional[str],
    details: dict,
    *,
    status: str,
    approved_by: Optional[str],
) -> None:
    """Persist a gate-audit ``approvals`` row for a cc_edit Gate decision.

    Mirrors ``join._audit_gate_results`` (asyncpg via ``core.db.get_pool``), and is
    likewise **fail-OPEN**: a missing pool / user_id / write error is logged and
    swallowed so the audit trail never breaks a turn. ``status='approved'`` +
    ``approved_by='gate'`` records an auto-cleared in-scope edit.
    """
    from uuid import UUID

    if not user_id:
        logger.debug("Skipping cc_edit gate audit — no user_id on the turn")
        return
    try:
        from core.db import get_pool

        pool = get_pool()
    except Exception as exc:  # noqa: BLE001
        logger.warning("cc_edit gate audit skipped — Postgres pool unavailable: %s", exc)
        return
    conv_uuid = None
    if conversation_id:
        try:
            conv_uuid = UUID(str(conversation_id))
        except (ValueError, TypeError):
            conv_uuid = None
    audit_details = {
        "file_paths": details.get("file_paths") or [],
        "scope": details.get("scope") or {},
        "summary": details.get("summary"),
    }
    try:
        await pool.execute(
            """
            INSERT INTO approvals (
                conversation_id, user_id, action_type, details, status, approved_by
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            conv_uuid, user_id, "cc_edit", json.dumps(audit_details), status, approved_by,
        )
    except Exception as exc:  # noqa: BLE001 — fail open
        logger.warning("cc_edit gate audit write failed: %s", exc)


async def _auto_apply_gate_cc_edit(
    combined_diff: str,
    file_paths: list,
    details: dict,
    conversation_id: Optional[str],
    user_id: Optional[str],
) -> dict:
    """Apply a Gate-auto-cleared cc_edit core-direct + write the audit row.

    Always records the ``approved_by='gate'`` audit row (the decision happened). The
    APPLY itself is gated by ``CC_EDIT_APPLY_ENABLED`` (default off): when disabled the
    edit is audited + captured but NOT written (parity with the human-approve path). On
    an apply failure the tree is left untouched (``apply_cc_edit`` is whole-or-nothing)
    and the error is surfaced, never masked.
    """
    from core.nodes.cc_edit import CCApplyError, apply_cc_edit

    await _audit_cc_edit_gate(
        user_id, conversation_id, details, status="approved", approved_by="gate",
    )
    base = {"approval_required": False, "approval_response": None, "error": None}
    targets = ", ".join(file_paths) or "the proposed diff"

    if not config.CC_EDIT_APPLY_ENABLED:
        return {
            **base,
            "messages": [{
                "role": "system",
                "content": (f"Code edit auto-cleared by the Gate (in scope, "
                            f"approved_by=gate) but apply is disabled "
                            f"(CC_EDIT_APPLY_ENABLED=false) — diff captured, not applied."),
            }],
        }
    try:
        # Pass the Gate-vetted path set so the apply primitive can reject any file git
        # would touch that the Gate never saw (F1 parser-differential belt-and-braces).
        apply_cc_edit(combined_diff, gated_paths=file_paths)
    except CCApplyError as exc:
        return {
            **base,
            "error": f"gate-cleared cc_edit failed to apply: {exc}",
            "messages": [{
                "role": "system",
                "content": f"Gate-cleared code edit failed to apply (tree untouched): {exc}",
            }],
        }
    return {
        **base,
        "messages": [{
            "role": "system",
            "content": f"Code edit auto-applied by the Gate (in scope, approved_by=gate): {targets}.",
        }],
    }


def _resolve_cc_edit(
    pending_edit: dict, approval_response: str, state: "AgentState"
) -> dict:
    """Apply or drop a parked ``cc_edit`` per the user's decision (C4 step 3)."""
    from core.nodes.cc_edit import CCApplyError, apply_cc_edit

    decision = str(approval_response).strip().lower()
    base = {
        "approval_required": False,
        "approval_response": None,
        "cc_pending_edit": None,
        "error": None,
    }

    if decision in {"reject", "rejected"}:
        return {
            **base,
            "messages": [
                {"role": "system", "content": "Proposed code edit rejected — not applied."}
            ],
        }

    # Approve. Honour an edit_content override (Travis tweaked the diff).
    diff = (state.get("approval_edit_content") or "").strip() or pending_edit.get("diff", "")

    if not config.CC_EDIT_APPLY_ENABLED:
        return {
            **base,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Code edit approved but apply is disabled "
                        "(CC_EDIT_APPLY_ENABLED=false) — diff captured, not applied."
                    ),
                }
            ],
        }

    try:
        apply_cc_edit(diff, config.CC_PROJECT_DIR)
    except CCApplyError as exc:
        return {
            **base,
            "messages": [
                {"role": "system", "content": f"Code edit could not be applied: {exc}"}
            ],
            "error": (str(exc) or type(exc).__name__),
        }
    return {
        **base,
        "messages": [
            {"role": "system", "content": "Approved code edit applied to the working tree (no commit)."}
        ],
    }

# Module-level router — replace in tests via monkeypatch
_router = LocalModelRouter()

# Injectable ChatOpenAI builder for the tool loop; tests patch this symbol.
# Signature: (backend: str) -> ChatOpenAI
_build_tool_model: Callable[[str], "ChatOpenAI"] = build_chat_openai

# Hard ceiling on the number of tool-call round-trips per turn.
_MAX_TOOL_ITERATIONS: int = 5  # kept for older imports; the live bound is
                                 # config.TOOL_LOOP_MAX_ROUNDS (default 25)

# ── Escalation pins (Redis-backed) ─────────────────────────────────────────
# Shared across all bobclaw-core workers via Redis key TTL.
_PIN_TTL_SECONDS: int = 1800

_redis_client: aioredis.Redis | None = None
_redis_warned_first_failure: bool = False


def _get_redis() -> aioredis.Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis_client


def _pin_key(backend: str) -> str:
    return f"bobclaw:pin:{backend}"


async def _pin_escalation(
    backend: str, pinned: str, ttl_seconds: int = _PIN_TTL_SECONDS,
) -> None:
    """Pin *backend* to route through *pinned* for the next *ttl_seconds*.

    Silently no-ops on Redis failure (logs a warning) — accepting the
    consistency hit per the multi-process degradation policy.
    """
    global _redis_warned_first_failure
    try:
        await _get_redis().set(_pin_key(backend), pinned, ex=ttl_seconds)
    except Exception as exc:
        if not _redis_warned_first_failure:
            _redis_warned_first_failure = True
            logger.warning(
                "Redis pin write failed for backend=%r; pin not persisted: %s",
                backend, exc,
            )
        else:
            logger.debug(
                "Redis pin write failed for backend=%r; pin not persisted: %s",
                backend, exc,
            )


async def _check_escalation_pin(backend: str) -> str | None:
    """Return the pinned backend if a valid pin exists, else None.

    Treats Redis failure as 'no pin' (logs a warning once per process,
    then DEBUG).
    """
    global _redis_warned_first_failure
    try:
        value = await _get_redis().get(_pin_key(backend))
    except Exception as exc:
        if not _redis_warned_first_failure:
            _redis_warned_first_failure = True
            logger.warning(
                "Redis pin read failed for backend=%r; treating as unpinned: %s",
                backend, exc,
            )
        else:
            logger.debug(
                "Redis pin read failed for backend=%r; treating as unpinned: %s",
                backend, exc,
            )
        return None
    return value


# ── Tool-loop helpers (P0) ─────────────────────────────────────────────────────

_TOOL_PREVIEW_CHARS = 400


def _tool_preview(value) -> str:
    """A bounded, human-readable preview of a tool argument or result."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = (text or "").strip().replace("\r", "")
    return text if len(text) <= _TOOL_PREVIEW_CHARS else text[:_TOOL_PREVIEW_CHARS] + "…"


def _emit_tool_activity(writer, **payload) -> None:
    """Publish one tool-loop frame on the graph's custom stream.

    The portal renders these as a live activity trail (S1, 2026-10-27). Emission is
    best-effort: a writer fault must never fail the turn, because the tool result
    still has to reach the model.
    """
    if writer is None:
        return
    try:
        writer({"type": "tool_activity", **payload})
    except Exception:
        logger.debug("tool activity writer raised; continuing", exc_info=True)


def _is_tool_enabled(face_id: str, backend: str) -> bool:
    """Return True when the active face/back-end pair should use the tool loop.

    The loop is opt-in: it fires only when
      1. the backend has a validated tool adapter (including worker Claude), and
      2. the face's allowed_tools contain at least one registered native tool
         or an explicit MCP tool ID (``mcp__*``).
    Existing faces use abstract labels ("search", "files", ...) that do not
    match native or MCP tool IDs, so they continue down the normal non-tool path.
    """
    if not tool_backend(backend):
        return False
    try:
        from core.faces.registry import get_default_registry

        registry = get_default_registry()
        allowed = registry.get_allowed_tools(face_id)
    except Exception:
        return False
    # Filter through the real gate so a face cannot accidentally bind a tool
    # it is not allowed to call.
    permitted = [t for t in allowed if check_tool_access(face_id, t)]
    # Enable when any permitted tool is native or is explicitly namespaced as MCP.
    return any(t in NATIVE_TOOLS or t.startswith("mcp__") for t in permitted)


def _dict_messages_to_lc(messages: list[dict]) -> list:
    """Convert our plain dict messages to LangChain message objects."""
    out: list = []
    for m in messages:
        role = m.get("role")
        content = m.get("content") or ""
        if role == "system":
            out.append(SystemMessage(content=content))
        elif role == "user":
            out.append(HumanMessage(content=content))
        elif role == "tool":
            out.append(
                ToolMessage(
                    content=content,
                    tool_call_id=m.get("tool_call_id", ""),
                    name=m.get("name", ""),
                )
            )
        else:
            # Assistant (or any unknown role) defaults to AIMessage.
            ai = AIMessage(content=content)
            tool_calls = m.get("tool_calls")
            if tool_calls:
                ai.tool_calls = tool_calls
            out.append(ai)
    return out


def _finish_tool_reply(content, backend, writer, fallback):
    # Some providers return structured content, including reasoning-only blocks.
    # Only visible text belongs in the saved/displayed assistant reply.
    if isinstance(content, list):
        content = "".join(block.get("text", "") for block in content
                          if isinstance(block, dict) and block.get("type") in {"text", "output_text"}
                          and isinstance(block.get("text"), str))
    if not isinstance(content, str) or not content.strip():
        content = fallback
    if writer is not None:
        try:
            writer({"type": "token", "content": content, "backend": backend, "model": None})
        except Exception:
            logger.debug("tool-loop stream writer raised; continuing", exc_info=True)
    return content


class ToolLoopReply(str):
    def __new__(cls, content, step_extensions=0):
        reply = super().__new__(cls, content)
        reply.step_extensions = step_extensions
        return reply


class ToolRoundLimitReply(ToolLoopReply):
    """Visible partial reply with a machine-readable reason for stopping."""
    stopped_reason = "tool_round_limit"


class ProviderStoppedReply(ToolLoopReply):
    def __new__(cls, content, failure, step_extensions=0):
        reply = super().__new__(cls, content, step_extensions)
        reply.stopped_reason = failure.code
        return reply


async def _run_tool_loop(
    messages: list[dict],
    backend: str,
    face_id: str,
    writer,
    model_override=None,
    allow_step_questions=False,
) -> str:
    """Bounded tool-calling loop for a tool-enabled face.

    1. Bind only the face's permitted native + MCP tools.
    2. Call the model; if it emits tool_calls, execute them directly via the
       tools' ``ainvoke`` methods, append the results, and re-call.
    3. When no tool_calls are emitted (or the iteration cap is hit), return
       the final assistant content.
    """
    from core.faces.registry import get_default_registry

    registry = get_default_registry()
    from core.execution.workflow_context import current as workflow_current
    workflow = workflow_current.get()
    allowed = registry.get_allowed_tools(face_id)
    permitted = [t for t in allowed if check_tool_access(face_id, t)]
    tools = await get_all_tools(permitted)
    if workflow: tools = workflow.filter_tools(tools)
    if not tools and not workflow and backend != 'claude_code':
        raise RuntimeError(f"No bound tools for face '{face_id}'")

    from core.faces.prompt import interactive_messages
    messages = (workflow.messages(messages, tools) if workflow else
                interactive_messages(messages, face_id, backend, tools))
    summaries = workflow.skill_summaries(registry) if workflow else registry.list_skills(face_id)
    if summaries:
        messages = [*messages, {"role": "system", "content":
                    "Available skill summaries (load with skill_view): " + json.dumps(summaries)}]
    tools_by_name = {t.name: t for t in tools}
    cli_turn = backend == 'claude_code' and tool_backend(backend)
    if cli_turn:
        from core.backends.claude_turn import ClaudeTurn
        from core.lane_config import get_lane
        model = ClaudeTurn(model=model_override or get_lane().backends[backend].model, tools=tools, writer=writer)
    else:
        base_model = _build_tool_model(backend, model_override=model_override) if model_override else _build_tool_model(backend)
        model = base_model.bind_tools(tools) if tools else base_model
    # Same call-time guard as _stream_and_collect: the final backend is known here.
    from core.context import enforce_backend_limits

    require_backend(backend, model_override) if model_override else require_backend(backend)
    fitted_messages = enforce_backend_limits(messages, backend)
    if workflow and fitted_messages != messages:
        from core.execution.models import ExecutionError
        raise ExecutionError('workflow_input_too_large',413,'The frozen stage evidence does not fit this model. Narrow the task before starting a new run.')
    messages = fitted_messages
    lc_messages = _dict_messages_to_lc(messages)

    # One grant per turn: web_search records the hosts it surfaces and web_fetch
    # may read them for the rest of THIS turn only (see core/tools/web.py).
    from core.tools.web import begin_turn, end_turn

    search_turn = f"{face_id}:{uuid.uuid4().hex}"
    search_token = begin_turn(search_turn)
    from core.builder.service import turn_id as builder_turn
    builder_token = builder_turn.set(uuid.uuid4().hex)
    from core.tools.conv_tools import begin_history_turn, history_turn
    history_token = begin_history_turn()
    round_limit = registry.get_face(face_id).tool_loop_max_rounds or config.TOOL_LOOP_MAX_ROUNDS
    if workflow:
        round_limit = min(round_limit, workflow.round_limit)
    builder_receipts = []
    visible_parts = []
    completed_steps = []
    step_extensions = 0
    rounds = 0
    round_limit = max(1, int(round_limit))
    try:
        while True:
            if rounds >= round_limit * (step_extensions + 1) and not (cli_turn and model.more_tools_in_round):
                if workflow or writer is None or not allow_step_questions or step_extensions >= 3:
                    break
                from core.tools.ask import bind_writer, unbind_writer, request_step_extension, can_request_step_extension
                token = bind_writer(writer)
                # Publish progress even if the model has emitted no prose, so
                # the gateway stores a pre-question row before the owner's answer.
                try:
                    if not can_request_step_extension():
                        break
                    progress = f"I have used {rounds} steps on this reply and I am not finished.\n\n"
                    visible_parts.append(_finish_tool_reply(progress, backend, writer, ''))
                    started = perf_counter()
                    granted = await request_step_extension(rounds)
                    # The broker resumes its existing active-work clock on this
                    # same question-completion signal, including on timeout.
                    _emit_tool_activity(writer, phase="result", tool="ask_operator", ok=True,
                        elapsed_ms=int((perf_counter() - started) * 1000),
                        preview="The owner granted more steps." if granted else "The step question closed without an extension.")
                finally:
                    unbind_writer(token)
                if not granted:
                    break
                step_extensions += 1
            continuing_tool_reply = bool(cli_turn and model.more_tools_in_round)
            try:
                if continuing_tool_reply:
                    # A CLI assistant message can contain several tool calls.
                    # Their bridge requests belong to the already charged round.
                    response = await model.ainvoke(lc_messages)
                    if workflow:
                        workflow.turn_messages = lc_messages
                        workflow.last_response = response
                elif workflow:
                    response = await workflow.model_call(model, lc_messages)
                    rounds += 1
                else:
                    from core.execution.provider_retry import model_call

                    async def dispatch():
                        nonlocal rounds
                        rounds += 1  # A retry consumes the same turn call/step allowance.
                        return await model.ainvoke(lc_messages)

                    response = await model_call(dispatch, writer=writer,
                        can_retry=lambda: rounds < round_limit * (step_extensions + 1))
            except Exception as exc:
                from core.backends.provider_failure import describe_failure
                failure = describe_failure(exc)
                if failure is None:
                    raise
                if workflow:
                    if cli_turn:
                        from core.execution.models import ExecutionError
                        raise ExecutionError(failure.code, failure.status or 502, failure.sentence) from None
                    raise
                # No tool from this failed call has run. Keep earlier visible text
                # and the ordinary tool activity trail, without replaying effects.
                from collections import Counter
                facts = []
                if cli_turn and model.text:
                    # The CLI already streamed this text; add only the separator.
                    visible_parts.append(model.text + '\n\n')
                    model.text = ''
                    if writer:
                        writer({'type': 'token', 'content': '\n\n', 'backend': backend, 'model': model.actual})
                for ok, label in ((True, 'Completed tool steps'), (False, 'Tool steps that failed')):
                    counts = Counter(name.replace('_', ' ') for name, success in completed_steps if success == ok)
                    if counts:
                        facts.append(label + ': ' + ', '.join(f'{name} ({count})' for name, count in counts.items()) + '.')
                notice = ' '.join([*facts, failure.sentence, 'Reply "continue" to carry on from the saved progress.'])
                return ProviderStoppedReply(''.join(visible_parts) + _finish_tool_reply(
                    notice, backend, writer, notice), failure, step_extensions)
            if workflow and not continuing_tool_reply:
                workflow.begin_tool_reply()
            if backend == 'openai_codex' and writer:
                metadata = response.response_metadata or {}
                writer({'type': 'model_observation', 'backend': backend,
                    'model': metadata.get('requested_model') or model_override,
                    'provider_reported_model': metadata.get('model_name'), 'billing': 'subscription'})
                writer({'type': 'model_usage', 'backend': backend,
                    **(response.usage_metadata or {}), 'available': response.usage_metadata is not None,
                    'model': metadata.get('model_name'), 'billing': 'subscription'})
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                if workflow:
                    from core.builder.summary import assistant_text
                    workflow.final_text = assistant_text(response.content)
                fallback = "The model returned no visible answer. Please retry or inspect the tool activity."
                content = response.content
                if face_id == 'builder':
                    from core.builder.summary import summary, assistant_text
                    content = assistant_text(content)
                    fallback = summary(builder_receipts) or fallback
                return ToolLoopReply(''.join(visible_parts) + _finish_tool_reply(content, backend,
                    None if cli_turn and content else writer, fallback), step_extensions)

            # Providers may put the plan and a question tool call in one round.
            # Publish visible text BEFORE invoking tools (which may park the turn).
            from core.builder.summary import assistant_text
            preface = assistant_text(response.content)
            if preface:
                if cli_turn and writer:
                    writer({'type': 'token', 'content': '\n\n', 'backend': backend, 'model': model.actual})
                visible_parts.append(_finish_tool_reply(preface + '\n\n', backend, None if cli_turn else writer, ''))
            # Execute the requested tools manually and feed the results back.
            tool_msgs = []
            for tc in tool_calls:
                name, args, cid = tc["name"], tc.get("args", {}), tc.get("id", "")
                if cli_turn:
                    model.tool_started(name, args, cid)
                    if workflow and workflow.repairing:
                        workflow.repair_tool_calls = True
                if workflow:
                    from core.execution.agent_turn import ToolRefusal
                    try:
                        await workflow.before_tool(name, args)
                    except ToolRefusal as exc:
                        content = await workflow.refuse(name, exc, args)
                        tool_msgs.append(ToolMessage(content=content, tool_call_id=cid, name=name))
                        if cli_turn: model.accept_result(cid, content)
                        continue
                tool = tools_by_name.get(name)
                if cli_turn:
                    if not check_tool_access(face_id, name) or name not in registry.get_allowed_tools(face_id):
                        tool = None
                _emit_tool_activity(writer, phase="start", tool=name, args=_tool_preview(args))
                started = perf_counter()
                ok = True
                if tool is None:
                    ok = False
                    content = f"Error: tool '{name}' is not available"
                else:
                    # S2: bind the turn's writer so a tool can publish a mid-turn
                    # question (ask_operator) and wait for the operator's answer.
                    from core.tools.ask import bind_writer, unbind_writer
                    from core.tools.registry import _skill_face_id
                    skill_token = _skill_face_id.set(face_id)
                    token = bind_writer(writer)
                    try:
                        content = str(await tool.ainvoke(args))
                    except Exception as exc:
                        if workflow:
                            from core.execution.models import ExecutionError
                            from core.execution.agent_turn import TOOL_REFUSALS, ToolStopped
                            if not isinstance(exc,ExecutionError) or exc.code not in TOOL_REFUSALS:
                                if isinstance(exc,ExecutionError):
                                    raise ToolStopped(name,exc.code,exc.status) from None
                                raise
                            content = await workflow.refuse(name,exc,args)
                            tool_msgs.append(ToolMessage(content=content, tool_call_id=cid, name=name))
                            if cli_turn: model.accept_result(cid, content)
                            continue
                        ok = False
                        content = f"Error executing '{name}': {exc}"
                    finally:
                        unbind_writer(token)
                        _skill_face_id.reset(skill_token)
                if workflow:
                    try:
                        await workflow.tool_result(name, content)
                    except ToolRefusal as exc:
                        content = await workflow.refuse(name,exc,args)
                        ok = False
                    else:
                        workflow.tool_succeeded()
                _emit_tool_activity(
                    writer,
                    phase="result",
                    tool=name,
                    ok=ok,
                    elapsed_ms=int((perf_counter() - started) * 1000),
                    preview=_tool_preview(content),
                )
                if cli_turn: model.accept_result(cid, content)
                completed_steps.append((name, ok))
                tool_msgs.append(
                    ToolMessage(content=content, tool_call_id=cid, name=name)
                )
                if face_id == 'builder':
                    builder_receipts.append((name, content))
                    try:
                        result = json.loads(content)
                        end_builder_turn = result.get('end_turn') is True
                    except (ValueError, AttributeError):
                        end_builder_turn = False
                    if end_builder_turn:
                        from core.builder.summary import summary, assistant_text
                        report = assistant_text(result.get('report')) if name == 'task_finish' else ''
                        if workflow:
                            workflow.final_text = report or summary(builder_receipts)
                        return ToolLoopReply(''.join(visible_parts) + _finish_tool_reply(
                            report, backend, writer, summary(builder_receipts)), step_extensions)
            if workflow and not (cli_turn and model.more_tools_in_round):
                workflow.finish_tool_reply()
            lc_messages.append(response)
            lc_messages.extend(tool_msgs)
    except asyncio.CancelledError:
        if cli_turn and model.interrupted_tool_error is not None:
            asyncio.current_task().uncancel()
            if workflow:
                from core.backends.provider_failure import describe_failure
                from core.execution.models import ExecutionError
                failure = describe_failure(model.interrupted_tool_error)
                if failure:
                    raise ExecutionError(failure.code, failure.status or 502, failure.sentence) from None
            raise model.interrupted_tool_error from None
        raise
    finally:
        if cli_turn:
            await asyncio.shield(model.close())
        builder_turn.reset(builder_token)
        end_turn(search_turn, search_token)
        history_turn.reset(history_token)

    # The final tool-only response precedes the final results; it is not an
    # answer. Report bounded facts without another model call or more tools.
    if workflow:
        from core.execution.models import ExecutionError
        if workflow.last_refusal and workflow.refusals:
            raise ExecutionError('tool_refusal_limit',409,workflow.refusal_reason())
        raise ExecutionError('tool_round_limit', 409)
    # The model's visible progress text has already been streamed above. Keep it
    # in history, followed by an explicit unfinished notice, without raw results
    # or another model call beyond the existing limit.
    notice = ('I reached the limit of steps for one reply before finishing. '
              'Reply "continue" to carry on.')
    return ToolRoundLimitReply(''.join(visible_parts) + _finish_tool_reply(notice, backend, writer, notice), step_extensions)


# ── Backend transport ────────────────────────────────────────────────────────

def _cli_spawn_posture(model_override: Optional[str] = None) -> dict:
    """Posture for the STATELESS CLI-subprocess branches (fan-out workers,
    council seat sends, critics — anything behind the bare seam).

    Spawn-context intake 2026-07-18: clean scratch is the default for every
    stateless CLI spawn. Callers with a position of their own (council seats)
    publish a descriptor via ``spawn_descriptor``; absent that, the spawn is
    framed as a plain worker — never as an agent of whatever repo the process
    happens to run from.
    """
    from core.spawn_context import active_spawn_descriptor

    posture: dict = {"model": model_override} if model_override else {}
    posture["spawn_context"] = active_spawn_descriptor() or {"position": "worker"}
    return posture


def _face_spawn_posture(posture: dict, state: dict) -> dict:
    """Merge the face YAML ``spawn_context:`` block into a state-aware CLI
    posture (claude_code / agy_code / codex_code planner-tier dispatch).

    An explicit descriptor already in the posture wins; a face without the
    block leaves the posture untouched (legacy behavior — face authors opt in
    per face, e.g. planner-cc-edit's project-aware planner template).
    """
    if isinstance(posture.get("spawn_context"), dict) and posture["spawn_context"]:
        return posture
    try:
        from core.faces.registry import get_default_registry

        face = get_default_registry().get_face((state.get("face_id") or "").strip())
        descriptor = dict(face.spawn_context or {})
    except Exception:
        return posture
    if not descriptor:
        return posture
    return {**posture, "spawn_context": descriptor}


def _messages_to_prompt(messages: list[dict]) -> str:
    """Collapse a message list into the single prompt string the ``claude`` CLI
    takes via ``-p``.

    System blocks first (joined), then the conversation turns. The CLI already
    auto-loads ``CLAUDE.md`` + per-project memory on every spawn, so this stays
    minimal — it carries the splice context + the latest user turn, not a full
    re-statement of the project.
    """
    system_parts = [
        m.get("content", "")
        for m in messages
        if m.get("role") == "system" and m.get("content")
    ]
    convo_parts = [
        f"{m.get('role', 'user')}: {m.get('content', '')}"
        for m in messages
        if m.get("role") != "system" and m.get("content")
    ]
    sections: list[str] = []
    if system_parts:
        sections.append("\n\n".join(system_parts))
    if convo_parts:
        sections.append("\n\n".join(convo_parts))
    return "\n\n".join(sections).strip()


async def _default_send_to_backend(
    messages: list[dict],
    backend: str,
    model_override: Optional[str] = None,
) -> str:
    """Route messages to the appropriate backend and return the full response.

    Handles local backends (ollama / lmstudio) and cloud backends.

    The local branch threads *model_override* through to the router's chat
    call. State-aware callers (``execute_node``) read the override from
    ``state.model_override`` and pass it explicitly; stateless callers
    (``decompose._default_call_llm``) pass None. Two-arg call sites still
    work because the parameter has a default value.
    """
    require_backend(backend, model_override)
    if backend == "openai_codex":
        from core.backends.openai_codex import OpenAICodexClient
        raw = await OpenAICodexClient().chat(messages, model=model_override)
        return raw["choices"][0]["message"]["content"]
    if backend == "kimi_code":
        from core.backends.kimi import KimiClient

        client = KimiClient()
        raw = await client.chat(messages=messages, model=None)
        return raw["choices"][0]["message"]["content"]

    if backend == "kimi_platform":
        from core.backends.kimi_platform import KimiPlatformClient

        ok, total, mode = check_cap()
        if not ok:
            return (
                f"[Kimi Platform daily cap reached: ${total:.2f} / "
                f"${config.KIMI_PLATFORM_DAILY_USD_LIMIT:.2f}. "
                f"Retry tomorrow or raise the cap."
            )
        if mode == "warn":
            logger.warning(
                "Kimi PAYG daily spend $%.2f crossed warn threshold $%.2f (cap $%.2f)",
                total,
                config.KIMI_PLATFORM_DAILY_USD_WARN,
                config.KIMI_PLATFORM_DAILY_USD_LIMIT,
            )

        client = KimiPlatformClient()
        raw = await client.chat(messages=messages, model=None)
        usage = parse_usage(raw)
        track_cost(**usage)
        # L0.4: attribute this metered call's USD to the current flight (contextvar set
        # by the node) so the per-flight spend meter aggregates it. Best-effort.
        await record_current_flight_spend("kimi_platform", usd_for(**usage))
        return raw["choices"][0]["message"]["content"]

    if backend == "claude_api":
        from core.backends.claude import ClaudeClient

        client = ClaudeClient()
        system_msgs = [m for m in messages if m.get("role") == "system"]
        convo = [m for m in messages if m.get("role") != "system"]
        system_prompt = "\n\n".join(m["content"] for m in system_msgs) or None
        raw = await client.chat(
            messages=convo, model=None, system=system_prompt
        )
        return raw["content"][0]["text"]

    if backend == "deepseek_v4_flash":
        from core.backends.deepseek import DeepSeekClient

        client = DeepSeekClient()
        raw = await client.chat(messages=messages, model=None)
        return raw["choices"][0]["message"]["content"]

    if backend == "deepseek_v4":
        # DS Pro — senior/audit tier. Same DeepSeek endpoint, the Pro model id
        # (config.DEEPSEEK_PRO_MODEL). A per-request override still wins.
        from core.backends.deepseek import DeepSeekClient

        client = DeepSeekClient()
        raw = await client.chat(
            messages=messages, model=model_override or config.DEEPSEEK_PRO_MODEL
        )
        return raw["choices"][0]["message"]["content"]

    if backend == "qwen_cloud":
        from core.backends.qwen_cloud import QwenCloudClient
        raw = await QwenCloudClient().chat(messages=messages, model=model_override)
        return raw["choices"][0]["message"]["content"]

    if backend == "qwen_research":
        # MS2-R0: the self-hostable Qwen research floor (local llama.cpp, OpenAI-compat). Thread the
        # per-request model override through (the worker face's model alias / the served gguf id).
        from core.backends.qwen_research import QwenResearchClient

        client = QwenResearchClient()
        raw = await client.chat(messages=messages, model=model_override)
        return raw["choices"][0]["message"]["content"]

    if backend == "glm_5_3":
        # FU1: serial-GLM cross-flight mutex (inert unless FLIGHT_ENFORCE_ENABLED). On
        # contention, D1 back-off escalates to a parallel-safe backend instead of blocking.
        from core.flight.enforcement import acquire_or_escalate, release

        run_backend, holder = await acquire_or_escalate(backend)
        if run_backend != backend:
            return await _default_send_to_backend(messages, run_backend, model_override)
        from core.backends.glm import GLMClient

        client = GLMClient()
        try:
            raw = await client.chat(messages=messages, model=model_override)
        finally:
            await release(backend, holder)
        return raw["choices"][0]["message"]["content"]

    if backend == "glm_5_3_flash_or":
        # GLM 5.3 Flash through OpenRouter — PAYG, parallel-safe, no Z.AI serial-1 slot.
        from core.backends.glm import GLMClient

        client = GLMClient(
            base_url=config.OPENROUTER_BASE_URL, api_key=config.OPENROUTER_API_KEY,
            vault_provider=None,
        )
        raw = await client.chat(
            messages=messages, model=model_override or config.OPENROUTER_GLM_FLASH_MODEL
        )
        return raw["choices"][0]["message"]["content"]

    if backend == "minimax":
        import re

        from core.backends.minimax import MiniMaxClient

        client = MiniMaxClient()
        raw = await client.chat(messages=messages, model=None)
        content = raw["choices"][0]["message"]["content"] or ""
        # MiniMax-M3 is a reasoning model; strip the leading <think>...</think>
        # block so only the final answer reaches the user.
        return re.sub(r"^\s*<think>.*?</think>\s*", "", content, flags=re.DOTALL)

    if backend in ("gemini_flash", "gemini_pro", "gemini_deep_research"):
        from core.backends.gemini import GeminiClient

        model_map = {
            "gemini_flash": config.GEMINI_FLASH_MODEL,
            "gemini_pro": config.GEMINI_PRO_MODEL,
            "gemini_deep_research": config.GEMINI_DEEP_RESEARCH_MODEL,
        }
        client = GeminiClient()
        raw = await client.chat(messages=messages, model=model_map[backend])
        texts = [p["text"] for p in raw["candidates"][0]["content"]["parts"] if p.get("text")]
        return "\n".join(texts)

    if backend == "opencode_serve":
        from core.backends.opencode_pool import _pool

        # state-aware callers (execute_node) intercept opencode_serve before
        # this function is reached. This branch only runs if some future caller
        # invokes opencode_serve without state context, in which case "any
        # workspace" is a reasonable default.
        return await _pool.dispatch(messages, workspace_dir=None)

    if backend == "claude_code" and tool_backend(backend):
        from core.backends.claude_turn import text_stream
        return ''.join([delta async for delta in text_stream(messages, model_override)])

    if backend == "claude_code":
        from core.backends.claude_code import ClaudeCodeClient

        # State-aware callers (execute_node) intercept claude_code before this
        # function is reached and thread posture + resume_session_id via state.
        # This stateless path serves council seats / fan-out sends: clean-scratch
        # spawn context by default (worker framing, or the caller's published
        # descriptor — council seats), never the repo cwd + its charter.
        client = ClaudeCodeClient()
        result = await client.chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture=_cli_spawn_posture(),
        )
        return result["text"]

    if backend == "agy_code":
        from core.backends.agy_code import AntigravityClient

        # State-aware callers (execute_node) intercept agy_code before this
        # function; this stateless path serves the fan-out worker. The worker
        # face's model is threaded in as model_override (worker_node) so
        # worker-agy actually runs its configured model, not agy's default. A
        # unique uuid cwd is minted per client → fan-out safe.
        client = AntigravityClient()
        result = await client.chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture=_cli_spawn_posture(model_override),
        )
        return result["text"]

    if backend == "codex_code":
        from core.backends.codex_code import CodexCodeClient

        # Stateless path = the fan-out worker (worker-codex). The worker face's
        # litellm model name (glm-5.3 / deepseek-v4-flash / qwen3.7-max) is threaded
        # as model_override so the worker runs its configured provider via the local
        # LiteLLM proxy; absent ⇒ the codex base-config default. A unique uuid cwd is
        # minted per client → fan-out safe. (posture-aware planner = the state-aware
        # block in execute_node, CX-2/CX-3.)
        client = CodexCodeClient()
        result = await client.chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture=_codex_lane_posture(model_override),
        )
        return result["text"]

    if backend == "kimi_cli":
        from core.backends.kimi_cli import KimiCliClient

        # Kimi via its own CLI (membership login). The worker face's model alias is
        # threaded as model_override; absent ⇒ the kimi config default. The prompt
        # is an argv value (kimi -p) so callers must keep it bounded.
        client = KimiCliClient()
        result = await client.chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture=_cli_spawn_posture(model_override),
        )
        return result["text"]

    if backend == "pi_code":
        from core.backends.pi_code import PiCodeClient

        # Pi agentic CLI (GLM/DeepSeek/MiniMax, direct OpenAI-compat — no proxy). The
        # fan-out threads the worker's provider/model (e.g. "deepseek/deepseek-v4-flash")
        # as model_override; absent ⇒ pi's default provider. Tools OFF for the stateless
        # worker path (clean text). A unique scratch cwd per client → fan-out safe.
        client = PiCodeClient()
        result = await client.chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture={"model": model_override} if model_override else {},
        )
        return result["text"]

    # Local backends
    backends = await _router.discover()
    best = _router.get_best_backend(backends)
    if not best:
        return (
            "[No local backend available — start Ollama or LM Studio "
            "and retry, or configure a cloud backend.]"
        )

    try:
        full = ""
        async for chunk in _router.chat(
            messages, model=model_override, backend=best
        ):
            full += chunk
        return full
    except RuntimeError as exc:
        # Per-request model override was given but the backend can't honor it
        # (model not installed, or no backend available at all). Surface the
        # clean error string to the user instead of letting a 400 stack trace
        # out of the WS path. Routed through the same "[No local backend ...]"
        # pattern as the no-backend case so downstream consumers can match it.
        return f"[No local backend available: {exc}]"


# Injectable reference for tests
# Signature: (messages, backend, model_override=None) -> str
_send_to_backend: Callable[[list[dict], str, Optional[str]], Awaitable[str]] = (
    _default_send_to_backend
)


# ── Streaming transport (per-token deltas) ─────────────────────────────────────


class _ThinkStripper:
    """Stateful filter that removes a leading ``<think>...</think>`` block from
    a token stream.

    Mirrors the full-string regex used in ``_default_send_to_backend`` for
    MiniMax-M3, but works incrementally: the opening/closing tags may straddle
    several deltas, so we buffer until we can decide whether the stream starts
    with a reasoning block, then pass everything after ``</think>`` through.
    """

    def __init__(self) -> None:
        self._buf = ""
        self._done = False  # True once the block is resolved / passed

    def feed(self, delta: str) -> str:
        if self._done:
            return delta
        self._buf += delta
        stripped = self._buf.lstrip()
        if not stripped:
            return ""  # only whitespace so far
        if stripped.startswith("<think>"):
            end = self._buf.find("</think>")
            if end == -1:
                return ""  # still inside the reasoning block — keep buffering
            rest = self._buf[end + len("</think>"):]
            self._buf = ""
            self._done = True
            return rest.lstrip()
        if "<think>".startswith(stripped):
            return ""  # ambiguous partial prefix of "<think>" — keep buffering
        # Definitely not a reasoning block — flush what we have and pass through.
        out = self._buf
        self._buf = ""
        self._done = True
        return out

    def flush(self) -> str:
        """Emit any residual buffer once the stream ends.

        If a ``<think>`` block never closed, fall back to the same regex the
        full-string path uses so behaviour matches exactly.
        """
        if self._done:
            return ""
        import re

        out = re.sub(
            r"^\s*<think>.*?</think>\s*", "", self._buf, flags=re.DOTALL
        )
        self._buf = ""
        self._done = True
        return out


async def _default_stream_to_backend(
    messages: list[dict],
    backend: str,
    model_override: Optional[str] = None,
) -> AsyncIterator[str]:
    """Yield assistant text deltas for *backend* as they arrive.

    Drives each backend's native ``stream_chat`` (or the local router's
    streaming ``chat``). This is the transport behind ``execute_node``'s
    per-token streaming.

    Backends whose full-string path carries side effects that streaming can't
    reproduce delegate to ``_send_to_backend`` and yield the whole response as
    a single delta:

    * ``kimi_platform`` — daily cost cap + ``track_cost`` usage accounting.
    * ``opencode_serve`` — workspace-bound pool dispatch (no token stream).

    ``decompose``/fan-out keep calling ``_send_to_backend`` directly (full
    string), so their planning/worker output is never surfaced token-by-token.
    """
    require_backend(backend, model_override)
    if backend == "openai_codex":
        from core.backends.openai_codex import OpenAICodexClient
        async for delta in OpenAICodexClient().stream_chat(messages, model=model_override, writer=_get_stream_writer()):
            yield delta
        return
    if backend == "claude_api":
        from core.backends.claude import ClaudeClient

        system_msgs = [m for m in messages if m.get("role") == "system"]
        convo = [m for m in messages if m.get("role") != "system"]
        system_prompt = "\n\n".join(m["content"] for m in system_msgs) or None
        async for delta in ClaudeClient().stream_chat(
            convo, model=None, system=system_prompt
        ):
            yield delta
        return

    if backend == "deepseek_v4_flash":
        from core.backends.deepseek import DeepSeekClient

        async for delta in DeepSeekClient().stream_chat(messages, model=None):
            yield delta
        return

    if backend == "deepseek_v4":
        from core.backends.deepseek import DeepSeekClient

        async for delta in DeepSeekClient().stream_chat(
            messages, model=config.DEEPSEEK_PRO_MODEL
        ):
            yield delta
        return

    if backend == "qwen_cloud":
        from core.backends.qwen_cloud import QwenCloudClient
        async for delta in QwenCloudClient().stream_chat(messages, model=model_override):
            yield delta
        return

    if backend == "qwen_research":
        from core.backends.qwen_research import QwenResearchClient

        async for delta in QwenResearchClient().stream_chat(messages, model=None):
            yield delta
        return

    if backend == "glm_5_3":
        # FU1: serial-GLM cross-flight mutex (inert unless FLIGHT_ENFORCE_ENABLED). On
        # contention, D1 back-off streams from a parallel-safe backend instead of blocking.
        from core.flight.enforcement import acquire_or_escalate, release

        run_backend, holder = await acquire_or_escalate(backend)
        if run_backend != backend:
            async for delta in _default_stream_to_backend(messages, run_backend, model_override):
                yield delta
            return
        from core.backends.glm import GLMClient

        try:
            async for delta in GLMClient().stream_chat(messages, model=model_override):
                yield delta
        finally:
            await release(backend, holder)
        return

    if backend == "glm_5_3_flash_or":
        from core.backends.glm import GLMClient

        client = GLMClient(
            base_url=config.OPENROUTER_BASE_URL, api_key=config.OPENROUTER_API_KEY,
            vault_provider=None,
        )
        async for delta in client.stream_chat(
            messages, model=model_override or config.OPENROUTER_GLM_FLASH_MODEL
        ):
            yield delta
        return

    if backend == "kimi_code":
        from core.backends.kimi import KimiClient

        async for delta in KimiClient().stream_chat(messages, model=None):
            yield delta
        return

    if backend == "minimax":
        from core.backends.minimax import MiniMaxClient

        stripper = _ThinkStripper()
        async for delta in MiniMaxClient().stream_chat(messages, model=None):
            out = stripper.feed(delta)
            if out:
                yield out
        tail = stripper.flush()
        if tail:
            yield tail
        return

    if backend in ("gemini_flash", "gemini_pro", "gemini_deep_research"):
        from core.backends.gemini import GeminiClient

        model_map = {
            "gemini_flash": config.GEMINI_FLASH_MODEL,
            "gemini_pro": config.GEMINI_PRO_MODEL,
            "gemini_deep_research": config.GEMINI_DEEP_RESEARCH_MODEL,
        }
        async for delta in GeminiClient().stream_chat(
            messages, model=model_map[backend]
        ):
            yield delta
        return

    if backend == "claude_code" and tool_backend(backend):
        from core.backends.claude_turn import text_stream
        async for delta in text_stream(messages, model_override):
            yield delta
        return

    if backend == "claude_code":
        from core.backends.claude_code import ClaudeCodeClient

        # Message-level streaming (probe-confirmed: one whole text block per
        # assistant event, NOT token deltas). Drive stream_chat directly so the
        # UI surfaces CC replies live. Stateless fallback path: no resume
        # (execute_node's state-aware block threads that); clean-scratch spawn
        # context by default.
        async for delta in ClaudeCodeClient().stream_chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture=_cli_spawn_posture(),
        ):
            yield delta
        return

    if backend == "agy_code":
        from core.backends.agy_code import AntigravityClient

        # Message-level streaming (one whole block). Stateless fallback: no
        # resume; the worker model is threaded via model_override.
        async for delta in AntigravityClient().stream_chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture=_cli_spawn_posture(model_override),
        ):
            yield delta
        return

    if backend == "codex_code":
        from core.backends.codex_code import CodexCodeClient

        # Message-level streaming (one whole block). Stateless fallback: no resume;
        # the litellm model name is threaded via model_override.
        async for delta in CodexCodeClient().stream_chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture=_codex_lane_posture(model_override),
        ):
            yield delta
        return

    if backend == "kimi_cli":
        from core.backends.kimi_cli import KimiCliClient

        async for delta in KimiCliClient().stream_chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture=_cli_spawn_posture(model_override),
        ):
            yield delta
        return

    if backend == "pi_code":
        from core.backends.pi_code import PiCodeClient

        # Message-level streaming (one whole block). Stateless fallback: no resume;
        # the provider/model is threaded via model_override.
        async for delta in PiCodeClient().stream_chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture={"model": model_override} if model_override else {},
        ):
            yield delta
        return

    if backend in ("kimi_platform", "opencode_serve"):
        # Side-effectful / non-streaming backends: reuse the full-string path
        # (cost cap + track_cost; or workspace pool dispatch) and surface the
        # whole response as one delta.
        full = await _send_to_backend(messages, backend, model_override)
        if full:
            yield full
        return

    # Local backends (ollama / lmstudio) — true token streaming via the router.
    backends = await _router.discover()
    best = _router.get_best_backend(backends)
    if not best:
        yield (
            "[No local backend available — start Ollama or LM Studio "
            "and retry, or configure a cloud backend.]"
        )
        return
    try:
        async for delta in _router.chat(
            messages, model=model_override, backend=best
        ):
            yield delta
    except RuntimeError as exc:
        # Mirror _default_send_to_backend: surface a clean error string instead
        # of letting a 400 stack trace escape the stream.
        yield f"[No local backend available: {exc}]"


# Injectable reference for tests
# Signature: (messages, backend, model_override=None) -> AsyncIterator[str]
_stream_to_backend: Callable[..., AsyncIterator[str]] = _default_stream_to_backend


def _get_stream_writer():
    """Return LangGraph's custom stream writer, or None outside a stream ctx.

    ``get_stream_writer()`` raises when called outside a running graph (e.g.
    direct unit tests of ``execute_node``) and is a no-op when the run isn't
    subscribed to the ``custom`` stream mode. Either way, ``None`` means "don't
    emit deltas" and the caller still accumulates the full response.
    """
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:
        return None


async def _stream_and_collect(
    messages: list[dict],
    backend: str,
    model_override: Optional[str],
    writer,
) -> str:
    """Drive ``_stream_to_backend``, emit each delta via *writer*, return full text."""
    # The final backend is known here (after routing and any 429 fallback): trim the prior-conversation
    # block if this backend has a hard prompt cap (a tested local model, an argv-limited CLI).
    from core.context import enforce_backend_limits

    require_backend(backend, model_override)
    messages = enforce_backend_limits(messages, backend)
    parts: list[str] = []

    async def deltas():
        if backend == 'openai_codex':
            # Codex buffers until response.completed: nothing has been published
            # and no client tool can have executed when a call fails.
            from core.execution.call_guard import required
            from core.execution.provider_retry import model_call

            async def collect():
                return [d async for d in _stream_to_backend(messages, backend, model_override)]

            import asyncio
            from time import monotonic
            deadline = monotonic() + 180

            async def remaining():
                return deadline - monotonic()

            try:
                async with asyncio.timeout(180):
                    buffered = await model_call(collect, writer=writer, remaining=remaining,
                                                can_retry=lambda: not required.get())
            except TimeoutError:
                from core.backends.openai_codex import provider_failure
                raise provider_failure(event_type='transport.turn_timeout', fallback='model_provider_error') from None
            for delta in buffered:
                yield delta
        else:
            async for delta in _stream_to_backend(messages, backend, model_override):
                yield delta

    async for delta in deltas():
        if not delta:
            continue
        parts.append(delta)
        if writer is not None:
            try:
                writer(
                    {
                        "type": "token",
                        "content": delta,
                        "backend": backend,
                        "model": model_override,
                    }
                )
            except Exception:
                # A stream-writer failure must never abort generation.
                logger.debug("stream writer raised; continuing", exc_info=True)
    response = "".join(parts)
    if not response.strip():
        raise ValueError("empty_response: backend stream returned no usable text")
    return response


async def execute_node(state: "AgentState") -> dict:
    """LangGraph node: call the backend and accumulate the response."""
    state = {**state, 'turn_id': uuid.uuid4().hex}
    task = state.get("task", "")
    backend = state.get("backend", "local")
    if configured_mode():
        try:
            require_state(state)
        except LaneConfigurationError as exc:
            return await _execution_failure(state, exc)
    messages = list(state.get("messages", []))
    approval_response = state.get("approval_response")

    # L0.4: bind this turn's flight (two-tier) so a metered backend call inside this
    # node attributes its USD to the right flight via the spend contextvar. Task-local
    # (rebound each turn); the fan-out path binds per-worker in worker_node.
    bind_flight_from_state(state)

    # ── CC approved-edit apply path (C4) ────────────────────────────────────
    # If a planner-cc-edit turn parked a proposed diff and the user just made a
    # decision on it, resolve it here (BEFORE the generic reject/approve flow):
    #   reject → drop it, note in the convo, no repo write.
    #   approve → core-direct `git apply` against CC_PROJECT_DIR (no commit,
    #             whole-or-nothing), honouring an edit_content override.
    pending_edit = state.get("cc_pending_edit")
    if pending_edit and approval_response is not None:
        return _resolve_cc_edit(pending_edit, approval_response, state)

    # ── If the previous approval was a rejection, stop here ─────────────────
    if (approval_response or "").strip().lower() == "rejected":
        return {
            "messages": [
                {"role": "system", "content": "Action was rejected by the user."}
            ],
            "approval_required": False,
            "error": None,
        }

    # ── First-pass check: does this task need approval before we call LLM? ──
    # Only raise the flag when we haven't been approved yet.
    if approval_response is None and task_requires_approval(task):
        return {
            "messages": [
                {
                    "role": "system",
                    "content": (
                        f"⚠️  Task '{task[:120]}' requires human approval "
                        "(email, form, purchase, or dangerous shell command detected)."
                    ),
                }
            ],
            "approval_required": True,
            "error": None,
        }

    # ── Build the user turn if not already present ──────────────────────────
    if task and not any(
        m.get("role") == "user" and task in m.get("content", "")
        for m in messages
    ):
        messages.append({"role": "user", "content": task})

    # ── Memory splice (Sprint INT-1) ─────────────────────────────────────────
    recalled = state.get("recalled_facts") or []
    recalled_chunks = (
        state.get("recalled_chunks") or []
        if config.MEMORY_T0_RECALL_ENABLED
        else []
    )
    memory_lines = [f.body.get("text", "") for f in recalled]
    memory_lines.extend(chunk.content for chunk in recalled_chunks)
    if memory_lines:
        from core.prompt_memory import prior_context
        messages.insert(0, {"role": "system", "content": prior_context(memory_lines)})

    # Only an owner-scoped boolean about the starter, never the owner file.
    if state.get('face_id') == 'assistant' and not state.get('agent_service_single_turn'):
        from core.starter_memory import starter_empty_for_owner
        starter_empty = starter_empty_for_owner(state.get('user_id'))
        if starter_empty is not None:
            messages.insert(0, {'role': 'system', 'content':
                'Owner starter headings empty: ' + str(starter_empty).lower() +
                '. Offer onboarding only when welcome, once per conversation; honor any decline in history.'})

    # ── Project context splice (server-side projects) ────────────────────────
    # The conversation's project instructions, resolved by the gateway. Inserted
    # at the front so it frames the turn ahead of recalled facts. Reaches every
    # backend: HTTP backends via _stream_and_collect(messages, ...) and the
    # claude_code planner via _messages_to_prompt(messages).
    project_instructions = (state.get("project_instructions") or "").strip()
    if project_instructions:
        messages.insert(0, {"role": "system", "content": f"Project context:\n{project_instructions}"})
    # ── Ask-Bob helper bubble page-context splice (MS9 U5) ───────────────────
    # An additive front-adjacent system card carrying the screen the user is viewing —
    # the same identity-card pattern as the project splice above. FLAG-GATED: page_context_card
    # returns None (⇒ nothing inserted ⇒ byte-identical prompt) whenever PAGE_CONTEXT_ENABLED is
    # off OR no page_context rode in. Reaches every backend via the same path as the project card.
    _page_context_card = page_context_card(
        state.get("page_context"), enabled=config.PAGE_CONTEXT_ENABLED
    )
    if _page_context_card is not None:
        messages.insert(0, _page_context_card)
    # This is the ONE locale injection point, before existing context, covering the HTTP join
    # and the subprocess _messages_to_prompt (both concatenate role:"system" messages in order).
    # locale_directive_message() is the single source of the guard (None => English, never raises).
    _locale_directive = locale_directive_message(state.get("locale"))
    if _locale_directive is not None:
        messages.insert(0, _locale_directive)

    # ── Opt-in LangChain tool-calling loop (P0) ──────────────────────────────
    if state.get("agent_service_single_turn"):
        # One exact model call, no tools, TTL pins, hidden decomposition or fallback.
        try:
            if not configured_mode() and backend == "qwen_research" and state.get("model_override") == "qwen3.8-flash-next-iq3_xxs":
                from core.backends.local_strata import LocalStrataClient
                response = await LocalStrataClient().chat(messages)
                writer = _get_stream_writer()
                if writer is not None:
                    writer({"type": "token", "content": response,
                            "backend": backend, "model": state["model_override"]})
            elif not configured_mode() and backend == "claude_alternate" and state.get("model_override") in ("claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"):
                # Second PC — a DIFFERENT ACCOUNT with its own subscription. Opt-in
                # seat only: the client pins HOST, so choose_host() is never consulted
                # and this can never draw on the primary account (or vice versa).
                from core.backends.claude_pc import (
                    ClaudeAlternateHaikuClient, ClaudeAlternateSonnetClient, ClaudeAlternateOpusClient)
                _alternate_model = state["model_override"]
                _alternate_client = (ClaudeAlternateOpusClient() if _alternate_model == "claude-opus-5-5"
                                else ClaudeAlternateSonnetClient() if _alternate_model == "claude-sonnet-5-5"
                                else ClaudeAlternateHaikuClient())
                # Reasoning effort is per-turn; the client validates it against a
                # fixed set before it can reach a shell string.
                _alternate_client.EFFORT = (state.get("model_effort") or "").strip().lower()
                response = await _alternate_client.chat(_messages_to_prompt(messages))
                writer = _get_stream_writer()
                if writer is not None:
                    writer({"type": "token", "content": response,
                            "backend": backend, "model": _alternate_model})
            elif not configured_mode() and backend == "claude_code" and state.get("model_override") in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
                from core.backends.claude_pc import ClaudePCClient, ClaudePCSonnetClient, ClaudePCHaikuClient
                _pc_model = state["model_override"]
                _pc_client = (ClaudePCSonnetClient() if _pc_model == "claude-sonnet-5-5"
                              else ClaudePCHaikuClient() if _pc_model == "claude-haiku-5-5"
                              else ClaudePCClient())
                response = await _pc_client.chat(_messages_to_prompt(messages))
                writer = _get_stream_writer()
                if writer is not None:
                    writer({"type": "token", "content": response,
                            "backend": backend, "model": _pc_model})
            else:
                response = await _stream_and_collect(
                    messages, backend, state.get("model_override"), _get_stream_writer()
                )
            await _append_agent_turn_event(state, assistant_response=response)
            return {"messages": [{"role": "assistant", "content": response}],
                    "approval_required": False, "error": None}
        except Exception as exc:
            return await _execution_failure(state, exc)

    # Only fires for a tool-enabled face on a tool-capable backend. Every other
    # face/backend combination falls through to the existing transport below.
    if (_is_tool_enabled(state.get("face_id", "assistant"), backend)
            or backend == 'claude_code' and tool_backend(backend)):
        writer = _get_stream_writer()
        from core.execution.chat_context import bind as bind_work, current as work_current
        work_token = bind_work(state)
        from core.tools.workflow_context import bind as bind_workflow, current as workflow_current
        workflow_token = bind_workflow(state)
        from core.execution.pc_context import bind as bind_pc, current as pc_current
        pc_token = bind_pc(state)
        user_token = _current_user_id.set(state.get("user_id"))
        conv_token = _current_conversation_id.set(state.get("conversation_id"))
        from core.execution.chat_records import request_id as chat_request_id
        # Older clients may omit a request id. Give an askable owner turn an
        # internal correlation id; the platform question carries it to the
        # gateway before any split history rows are saved.
        question_request_id = state.get("request_id")
        if (not question_request_id and state.get('step_questions_allowed') is True
                and writer is not None and state.get('user_id') and state.get('conversation_id')):
            question_request_id = uuid.uuid4().hex
        request_token = chat_request_id.set(question_request_id)
        # Provenance for the face-callable memory tools (F2, 2026-10-01).
        m_face_token = memory_tool_face_id.set(state.get("face_id", ""))
        m_user_token = memory_tool_user_id.set(state.get("user_id"))
        m_conv_token = memory_tool_conversation_id.set(
            state.get("conversation_id") or ""
        )
        try:
            response = await _run_tool_loop(
                messages, backend, state.get("face_id", "assistant"), writer,
                model_override=state.get('model_override'),
                allow_step_questions=state.get('step_questions_allowed') is True,
            )
            await _append_agent_turn_event(state, assistant_response=response)
            return {
                "messages": [{"role": "assistant", "content": response}],
                "approval_required": False,
                "approval_response": None,
                "error": None,
                **({"stopped_reason": response.stopped_reason} if isinstance(response, (ToolRoundLimitReply, ProviderStoppedReply)) else {}),
                "step_extensions": getattr(response, "step_extensions", 0),
            }
        except Exception as exc:
            return await _execution_failure(state, exc)
        finally:
            _current_user_id.reset(user_token)
            work_current.reset(work_token)
            workflow_current.reset(workflow_token)
            pc_current.reset(pc_token)
            _current_conversation_id.reset(conv_token)
            chat_request_id.reset(request_token)
            memory_tool_face_id.reset(m_face_token)
            memory_tool_user_id.reset(m_user_token)
            memory_tool_conversation_id.reset(m_conv_token)

    # Stateless service calls returned above. Tool calls assemble their card from
    # the actual bound schemas inside _run_tool_loop; other interactive transports
    # bind no native tools. Neither block is returned as a state/history delta.
    from core.faces.prompt import interactive_messages
    messages = interactive_messages(messages, state.get("face_id") or "assistant", backend)

    # ── Workspace-aware OpenCode dispatch ────────────────────────────────────
    if backend == "opencode_serve":
        from core.backends.opencode_pool import (
            _pool,
            NoOpenCodeAvailable,
        )

        workspace = state.get("workspace_dir")
        try:
            response = await _pool.dispatch(
                messages,
                workspace_dir=workspace,
            )
            await _append_agent_turn_event(state, assistant_response=response)
            return {
                "messages": [{"role": "assistant", "content": response}],
                "approval_required": False,
                "approval_response": None,
                "error": None,
            }
        except NoOpenCodeAvailable as exc:
            # No registered instance for this workspace — retarget to the
            # face's escalation_backend and fall through to standard send.
            if configured_mode() and not _eligible_fallback(state):
                return await _execution_failure(state, exc)
            backend = state.get("escalation_backend") or _execute_fallbacks.opencode_unavailable

    # ── State-aware Claude Code dispatch (planning tier) ─────────────────────
    # CC needs per-call state the stateless transport can't see: the resume
    # session id (continuity) and the face posture (CLI flag policy).
    if backend == "claude_code":
        from core.backends.claude_code import (
            ClaudeCodeClient,
            ClaudeCodeThrottled,
        )
        from core.cc_sessions import _lookup_cc_session, _record_cc_session

        posture = state.get("cc_posture") or {}          # C2 puts the face's flags here
        posture = _face_spawn_posture(posture, state)    # face spawn_context: block
        conversation_id = (state.get("conversation_id") or "").strip()
        resume_id = state.get("cc_resume_session_id")
        # Continuity (C3): resume this conversation's prior CC session if known.
        if conversation_id and not resume_id:
            resume_id = await _lookup_cc_session(conversation_id)
        # conversation_id also keys the per-conversation scratch dir (C2.1);
        # fall back to the resume id so scratch stays stable within a session.
        client = ClaudeCodeClient(
            cwd=config.CC_PROJECT_DIR,
            posture=posture,
            conversation_id=conversation_id or resume_id,
        )
        try:
            result = await client.chat(
                prompt=_messages_to_prompt(messages),
                resume_session_id=resume_id,
                posture=posture,
            )
            response = result["text"]
            # The server's "updates" SSE relay SKIPS re-emitting execute_node's
            # assistant message (it assumes the text was already token-streamed
            # via the "custom" channel). claude_code uses chat() — no token
            # writer — so emit the whole reply as ONE message-level "custom"
            # chunk here, or it never reaches the client. (Found in the
            # 2026-06-16 live E2E: short CC turns through execute_node returned
            # empty; matches the probe-confirmed message-level streaming model.)
            if response:
                _writer = _get_stream_writer()
                if _writer is not None:
                    try:
                        _writer({
                            "type": "token",
                            "content": response,
                            "backend": "claude_code",
                            "model": None,
                        })
                    except Exception:
                        logger.debug("cc stream writer raised; continuing", exc_info=True)
        except ClaudeCodeThrottled as exc:
            # claude_code threw a throttle classification → fall through to the face's
            # escalation backend (= claude_api after Decision 1), mirroring the
            # opencode NoOpenCodeAvailable retarget + the kimi 429 path.
            if configured_mode() and not _eligible_fallback(state):
                return await _execution_failure(state, exc)
            backend = state.get("escalation_backend") or _execute_fallbacks.claude_throttled
            # Was SILENT — log it so a "why didn't my edit apply" is diagnosable.
            logger.warning("claude_code throttled (%s); escalating to %s", exc, backend)
            # The proposed EDIT is a file on disk (proposed_*.diff), independent of the
            # chat reply. If a scratch-write planning turn already wrote one, CAPTURE it
            # here rather than losing it to the escalation backend (which has no scratch
            # dir). Only fall through to the escalation reply when there is no pending
            # edit to surface.
            if _cc_is_scratch_write(posture):
                cc_approval = await _maybe_capture_cc_edit(
                    client, posture, "", conversation_id, None,
                    state.get("scope"), state.get("user_id"),
                )
                if cc_approval is not None:
                    note = ("claude_code throttled before replying, but a proposed edit "
                            "was captured from the planning scratch dir.")
                    await _append_agent_turn_event(state, assistant_response=note)
                    out = {
                        "messages": [{"role": "assistant", "content": note}],
                        "approval_required": False,
                        "approval_response": None,
                        "error": None,
                    }
                    out.update(cc_approval)
                    return out
        else:
            session_id = client.last_session_id or result.get("session_id")
            if conversation_id and session_id:
                await _record_cc_session(
                    conversation_id,
                    session_id,
                    config.CC_PROJECT_DIR,
                )
            await _append_agent_turn_event(state, assistant_response=response)
            out = {
                "messages": [{"role": "assistant", "content": response}],
                "approval_required": False,
                "approval_response": None,
                "error": None,
            }
            if session_id:
                out["cc_resume_session_id"] = session_id

            # ── C4: capture a proposed edit from a scratch-write planner turn ──
            # planner-cc-edit runs the scratch-write posture and writes the
            # proposed unified diff to proposed_<n>.diff in its scratch dir.
            # Reuse the client's own scratch-path derivation (no duplication).
            cc_approval = await _maybe_capture_cc_edit(
                client, posture, response, conversation_id, session_id,
                state.get("scope"), state.get("user_id"),
            )
            if cc_approval is not None:
                out.update(cc_approval)
            return out

    # ── State-aware Antigravity (agy) dispatch (Gemini Second Voice) ──────────
    # Mirrors the claude_code block: per-call posture + resume continuity the
    # stateless transport can't see. agy owns the conversation uuid (we capture
    # it after the turn), so resume is by that uuid via the agy_sessions sidecar.
    if backend == "agy_code":
        from core.backends.agy_code import AntigravityClient, AgyError, AgyThrottled
        from core.agy_sessions import _lookup_agy_session, _record_agy_session

        posture = state.get("agy_posture")
        if posture is None:
            # agy_posture is a static per-face copy; read it directly (route does
            # not thread it, unlike cc_posture).
            try:
                from core.faces.registry import get_default_registry
                face = get_default_registry().get_face(state.get("face_id") or "")
                posture = dict(face.agy_posture or {})
            except Exception:
                posture = {}
        posture = _face_spawn_posture(posture, state)  # face spawn_context: block
        conversation_id = (state.get("conversation_id") or "").strip()
        resume_id = state.get("agy_resume_session_id")
        if conversation_id and not resume_id:
            resume_id = await _lookup_agy_session(conversation_id)
        client = AntigravityClient(
            cwd=config.AGY_PROJECT_DIR,
            posture=posture,
            conversation_id=conversation_id or resume_id,
        )
        try:
            result = await client.chat(
                prompt=_messages_to_prompt(messages),
                resume_session_id=resume_id,
                posture=posture,
            )
            response = result["text"]
            # Same message-level surfacing as claude_code: the SSE relay skips
            # re-emitting execute_node's assistant message, so emit the whole
            # reply as one "custom" chunk here.
            if response:
                _writer = _get_stream_writer()
                if _writer is not None:
                    try:
                        _writer({
                            "type": "token",
                            "content": response,
                            "backend": "agy_code",
                            "model": posture.get("model"),
                        })
                    except Exception:
                        logger.debug("agy stream writer raised; continuing", exc_info=True)
        except AgyThrottled as exc:
            # Subscription quota spent → fall through to the face's escalation
            # backend (= gemini_pro, the metered REST twin).
            if configured_mode() and not _eligible_fallback(state):
                return await _execution_failure(state, exc)
            backend = state.get("escalation_backend") or _execute_fallbacks.agy_throttled
        except AgyError as exc:
            # Non-throttle failure (timeout, missing binary, or a capture race
            # that survived the retry). Degrade gracefully like the generic /
            # tool-loop paths instead of letting it escape execute_node.
            logger.warning("agy_code turn failed: %s", exc)
            msg = f"Execution error: {exc}"
            await _append_agent_turn_event(state, assistant_response=msg)
            return {
                "messages": [{"role": "assistant", "content": msg}],
                "approval_required": False,
                "approval_response": None,
                "error": (str(exc) or type(exc).__name__),
            }
        else:
            agy_uuid = client.last_session_id or result.get("session_id")
            if conversation_id and agy_uuid:
                await _record_agy_session(conversation_id, agy_uuid)
            await _append_agent_turn_event(state, assistant_response=response)
            out = {
                "messages": [{"role": "assistant", "content": response}],
                "approval_required": False,
                "approval_response": None,
                "error": None,
            }
            if agy_uuid:
                out["agy_resume_session_id"] = agy_uuid
            return out

    # ── State-aware Codex (codex_code) dispatch ──────────────────────────────
    # Mirrors agy_code: per-call posture (profile / brief / scratch-write) +
    # resume continuity the stateless fan-out transport can't see. codex owns the
    # thread_id (captured after the turn), resumed via the codex_sessions sidecar.
    # The planner-codex face's posture (profile glm + brief) BINDS here.
    if backend == "codex_code":
        from core.backends.codex_code import CodexCodeClient, CodexError, CodexThrottled
        from core.codex_sessions import _lookup_codex_session, _record_codex_session

        posture = state.get("codex_posture")
        if posture is None:
            # codex_posture is a static per-face copy; read it directly (route does
            # not thread it, like agy_posture).
            try:
                from core.faces.registry import get_default_registry
                face = get_default_registry().get_face(state.get("face_id") or "")
                posture = dict(face.codex_posture or {})
            except Exception:
                posture = {}
        if configured_mode():
            posture = {**posture, "profile": get_lane().values.get("CODEX_PROFILE", "gpt")}
            configured_model = get_lane().require("codex_code").model
            if configured_model:
                posture.setdefault("model", configured_model)
        posture = _face_spawn_posture(posture, state)  # face spawn_context: block
        conversation_id = (state.get("conversation_id") or "").strip()
        resume_id = state.get("codex_resume_session_id")
        if conversation_id and not resume_id:
            resume_id = await _lookup_codex_session(conversation_id)
        client = CodexCodeClient(
            cwd=config.CODEX_PROJECT_DIR,
            posture=posture,
            conversation_id=conversation_id or resume_id,
        )
        try:
            result = await client.chat(
                prompt=_messages_to_prompt(messages),
                resume_session_id=resume_id,
                posture=posture,
            )
            response = result["text"]
            if response:
                _writer = _get_stream_writer()
                if _writer is not None:
                    try:
                        _writer({
                            "type": "token",
                            "content": response,
                            "backend": "codex_code",
                            "model": posture.get("model") or posture.get("profile"),
                        })
                    except Exception:
                        logger.debug("codex stream writer raised; continuing", exc_info=True)
        except CodexThrottled as exc:
            # Provider 429 via litellm → fall through to the face's escalation
            # backend (= opencode_serve: codex above opencode, opencode fallback).
            if configured_mode() and not _eligible_fallback(state):
                return await _execution_failure(state, exc)
            backend = state.get("escalation_backend") or _execute_fallbacks.codex_throttled
            logger.warning("codex_code throttled; escalating to %s", backend)
        except CodexError as exc:
            # Non-throttle failure (timeout, missing binary, proxy down). Degrade
            # gracefully like the agy / generic paths instead of escaping the node.
            logger.warning("codex_code turn failed: %s", exc)
            msg = f"Execution error: {exc}"
            await _append_agent_turn_event(state, assistant_response=msg)
            return {
                "messages": [{"role": "assistant", "content": msg}],
                "approval_required": False,
                "approval_response": None,
                "error": (str(exc) or type(exc).__name__),
            }
        else:
            thread_id = client.last_session_id or result.get("session_id")
            if conversation_id and thread_id:
                await _record_codex_session(conversation_id, thread_id)
            await _append_agent_turn_event(state, assistant_response=response)
            out = {
                "messages": [{"role": "assistant", "content": response}],
                "approval_required": False,
                "approval_response": None,
                "error": None,
            }
            if thread_id:
                out["codex_resume_session_id"] = thread_id

            # ── A1: capture a proposed edit from a codex scratch-write turn ──
            # planner-cc-edit-codex runs the scratch-write posture and writes the
            # proposed unified diff to proposed_<n>.diff in its work dir. Same
            # cc_edit Gate as claude_code (capture → scope-gate → core git apply),
            # reusing _maybe_capture_cc_edit unchanged (the work-dir accessor is
            # generalized to read CodexCodeClient._work_dir()).
            cc_approval = await _maybe_capture_cc_edit(
                client, posture, response, conversation_id, thread_id,
                state.get("scope"), state.get("user_id"),
            )
            if cc_approval is not None:
                out.update(cc_approval)
            return out

    # ── Call the backend (streamed, with 429 fallback) ───────────────────────
    # Tokens are emitted via LangGraph's custom stream writer as they arrive;
    # the full response is also accumulated for the L0 event + checkpoint state.
    # ``writer`` is None in non-streaming contexts (direct unit tests, or runs
    # not subscribed to stream_mode="custom") — generation still completes.
    model_override = state.get("model_override")
    writer = _get_stream_writer()
    try:
        pin = None if configured_mode() else await _check_escalation_pin(backend)
        effective_backend = pin or backend
        response = await _stream_and_collect(
            messages, effective_backend, model_override, writer
        )
        await _append_agent_turn_event(state, assistant_response=response)
        return {
            "messages": [{"role": "assistant", "content": response}],
            "approval_required": False,
            "approval_response": None,  # clear after use
            "error": None,
        }
    except aiohttp.ClientResponseError as exc:
        if exc.status == 429 and backend == "kimi_code":
            if configured_mode() and not _eligible_fallback(state):
                return await _execution_failure(state, exc)
            escalation = state.get("escalation_backend") or _execute_fallbacks.kimi_429
            await _pin_escalation("kimi_code", escalation, ttl_seconds=_PIN_TTL_SECONDS)
            response = await _stream_and_collect(
                messages, escalation, model_override, writer
            )
            await _append_agent_turn_event(state, assistant_response=response)
            return {
                "messages": [{"role": "assistant", "content": response}],
                "approval_required": False,
                "approval_response": None,  # clear after use
                "error": None,
            }
        if exc.status == 429 and backend == "pi_code":
            # pi CLI hit the Z.AI serial-1 ceiling (PiThrottled surfaces as a 429
            # ClientResponseError from the escalated send below). Escalate to the
            # face's direct-HTTP twin (e.g. glm_5_3) with a TTL pin, mirroring
            # the kimi_code path above.
            if configured_mode() and not _eligible_fallback(state):
                return await _execution_failure(state, exc)
            escalation = state.get("escalation_backend") or _execute_fallbacks.pi_429
            await _pin_escalation("pi_code", escalation, ttl_seconds=_PIN_TTL_SECONDS)
            response = await _stream_and_collect(
                messages, escalation, model_override, writer
            )
            await _append_agent_turn_event(state, assistant_response=response)
            return {
                "messages": [{"role": "assistant", "content": response}],
                "approval_required": False,
                "approval_response": None,  # clear after use
                "error": None,
            }
        return await _execution_failure(state, exc)
    except Exception as exc:
        # pi CLI unavailable (binary missing / spawn failed — PiError): retarget
        # to the face's direct-HTTP twin (e.g. glm_5_3), mirroring the
        # NoOpenCodeAvailable retarget. Without this, a pi-pinned face on a host
        # without the pi CLI is hard-down (state_error) even though its cloud
        # escalation twin is fully configured.
        try:
            from core.backends.pi_code import PiError
            is_pi_error = isinstance(exc, PiError)
        except Exception:
            is_pi_error = False
        if backend == "pi_code" and is_pi_error:
            if configured_mode() and not _eligible_fallback(state):
                return await _execution_failure(state, exc)
            escalation = state.get("escalation_backend") or _execute_fallbacks.pi_error
            logger.warning(
                "pi_code unavailable (%s); escalating to %s", exc, escalation,
            )
            try:
                response = await _stream_and_collect(
                    messages, escalation, model_override, writer
                )
                await _append_agent_turn_event(state, assistant_response=response)
                return {
                    "messages": [{"role": "assistant", "content": response}],
                    "approval_required": False,
                    "approval_response": None,
                    "error": None,
                }
            except Exception as esc_exc:
                return await _execution_failure(state, esc_exc)
        return await _execution_failure(state, exc)


async def _execution_failure(state: dict, exc: Exception) -> dict:
    tid = state.get('turn_id') or uuid.uuid4().hex
    state = {**state, 'turn_id': tid}
    from core.backends.provider_failure import describe_failure
    failure = describe_failure(exc)
    if failure is not None:
        failure.log()
        message = failure.sentence
        await _append_agent_turn_event(state, assistant_response=message, error_msg=failure.code)
        return {"messages": [{"role": "assistant", "content": message}],
                "error": failure.code, "error_type": failure.code, "turn_id": tid,
                "stopped_reason": failure.code,
                "approval_required": False, "approval_response": None}
    error = type(exc).__name__
    # Preserve stack frames without exception arguments, chains or message contents.
    safe = RuntimeError(error)
    logger.exception('Execution failed conversation_id=%s turn_id=%s face=%s backend=%s',
                     state.get('conversation_id'), tid, state.get('face_id'), state.get('backend'),
                     exc_info=(type(safe), safe, exc.__traceback__))
    face = 'Builder' if state.get('face_id') == 'builder' else 'Execution'
    message = f'{face} hit an internal error ({error}) — turn {tid}; details in the server log.'
    await _append_agent_turn_event(state, assistant_response=message, error_msg=error)
    return {"messages": [{"role": "assistant", "content": message}],
            "error": error, "error_type": error, "turn_id": tid,
            "approval_required": False, "approval_response": None}


def _eligible_fallback(state: dict) -> bool:
    fallback = state.get("escalation_backend")
    if not fallback:
        return False
    try:
        require_backend(fallback)
        return True
    except LaneConfigurationError:
        return False


def _codex_lane_posture(model_override: Optional[str] = None) -> dict:
    posture = _cli_spawn_posture(model_override)
    if configured_mode():
        lane = get_lane()
        posture["profile"] = lane.values.get("CODEX_PROFILE", "gpt")
        model = model_override or lane.require("codex_code").model
        if model:
            posture["model"] = model
    return posture
