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

import json
import logging
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Optional

import aiohttp
import redis.asyncio as aioredis

from core.backends._cost import check_cap, parse_usage, track_cost
from core.backends._lc_openai import TOOL_CAPABLE_BACKENDS, build_chat_openai
from core.backends.local_router import LocalModelRouter
from core.config import config
from core.nodes._l0_events import _append_agent_turn_event
from core.permissions import check_tool_access, requires_approval, task_requires_approval
from core.tools.projects import _current_conversation_id, _current_user_id
from core.tools.registry import NATIVE_TOOLS, get_all_tools

if TYPE_CHECKING:
    from core.graph import AgentState

logger = logging.getLogger(__name__)

# ── Locale response directive (i18n S0) ───────────────────────────────────────
# The ONE place the response-language directive text lives. Module-level so tests can
# import and assert the exact bytes (guards drift). Injected front-most in execute_node
# when state["locale"] is a known non-"en" locale.
LOCALE_DIRECTIVE = {
    "zh-Hans": "只用简体中文回答。无论用户消息或上下文使用何种语言，你的全部回复都必须是简体中文。",
    "zh-Hant": "只用繁體中文回答。無論使用者訊息或上下文使用何種語言，你的全部回覆都必須是繁體中文。",
}

def locale_directive_message(locale) -> Optional[dict]:
    """The front-most system directive for a response locale, or None to inject nothing.
    None / "en" / unknown / non-string => None (byte-identical English path). The SINGLE
    source of the locale-injection predicate; execute_node and the tests both call this."""
    if isinstance(locale, str) and locale != "en" and locale in LOCALE_DIRECTIVE:
        return {"role": "system", "content": LOCALE_DIRECTIVE[locale]}
    return None


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

    # Approve. Honour an edit_content override (the operator tweaked the diff).
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
            "error": str(exc),
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
_MAX_TOOL_ITERATIONS: int = 5

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

def _is_tool_enabled(face_id: str, backend: str) -> bool:
    """Return True when the active face/back-end pair should use the tool loop.

    The loop is opt-in: it fires only when
      1. the backend is wired through the ChatOpenAI wrapper, and
      2. the face's allowed_tools contain at least one registered native tool
         or an explicit MCP tool ID (``mcp__*``).
    Existing faces use abstract labels ("search", "files", ...) that do not
    match native or MCP tool IDs, so they continue down the normal non-tool path.
    """
    if backend not in TOOL_CAPABLE_BACKENDS:
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


async def _run_tool_loop(
    messages: list[dict],
    backend: str,
    face_id: str,
    writer,
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
    allowed = registry.get_allowed_tools(face_id)
    permitted = [t for t in allowed if check_tool_access(face_id, t)]
    tools = await get_all_tools(permitted)
    if not tools:
        raise RuntimeError(f"No bound tools for face '{face_id}'")

    tools_by_name = {t.name: t for t in tools}
    model = _build_tool_model(backend).bind_tools(tools)
    lc_messages = _dict_messages_to_lc(messages)

    for _ in range(_MAX_TOOL_ITERATIONS):
        response = await model.ainvoke(lc_messages)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            content = response.content or ""
            if writer is not None and content:
                try:
                    writer(
                        {
                            "type": "token",
                            "content": content,
                            "backend": backend,
                            "model": None,
                        }
                    )
                except Exception:
                    logger.debug("tool-loop stream writer raised; continuing", exc_info=True)
            return content

        # Execute the requested tools manually and feed the results back.
        tool_msgs = []
        for tc in tool_calls:
            name, args, cid = tc["name"], tc.get("args", {}), tc.get("id", "")
            tool = tools_by_name.get(name)
            if tool is None:
                content = f"Error: tool '{name}' is not available"
            else:
                try:
                    content = str(await tool.ainvoke(args))
                except Exception as exc:
                    content = f"Error executing '{name}': {exc}"
            tool_msgs.append(
                ToolMessage(content=content, tool_call_id=cid, name=name)
            )
        lc_messages.append(response)
        lc_messages.extend(tool_msgs)

    # Iteration cap hit while the model still wanted to call tools.
    # Return whatever the last response contained so the turn terminates.
    return response.content or ""


# ── Backend transport ────────────────────────────────────────────────────────

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

    if backend == "qwen_research":
        # MS2-R0: the self-hostable Qwen research floor (local llama.cpp, OpenAI-compat). Thread the
        # per-request model override through (the worker face's model alias / the served gguf id).
        from core.backends.qwen_research import QwenResearchClient

        client = QwenResearchClient()
        raw = await client.chat(messages=messages, model=model_override)
        return raw["choices"][0]["message"]["content"]

    if backend == "glm_5_2":
        from core.backends.glm import GLMClient

        client = GLMClient()
        raw = await client.chat(messages=messages, model=None)
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

    if backend == "claude_code":
        from core.backends.claude_code import ClaudeCodeClient

        # State-aware callers (execute_node) intercept claude_code before this
        # function is reached and thread posture + resume_session_id via state.
        # This stateless fallback uses defaults (no posture, no resume).
        client = ClaudeCodeClient()
        result = await client.chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture={},
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
            posture={"model": model_override} if model_override else {},
        )
        return result["text"]

    if backend == "codex_code":
        from core.backends.codex_code import CodexCodeClient

        # Stateless path = the fan-out worker (worker-codex). The worker face's
        # litellm model name (glm-5.2 / deepseek-v4-flash / qwen3.7-max) is threaded
        # as model_override so the worker runs its configured provider via the local
        # LiteLLM proxy; absent ⇒ the codex base-config default. A unique uuid cwd is
        # minted per client → fan-out safe. (posture-aware planner = the state-aware
        # block in execute_node, CX-2/CX-3.)
        client = CodexCodeClient()
        result = await client.chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture={"model": model_override} if model_override else {},
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

    if backend == "qwen_research":
        from core.backends.qwen_research import QwenResearchClient

        async for delta in QwenResearchClient().stream_chat(messages, model=None):
            yield delta
        return

    if backend == "glm_5_2":
        from core.backends.glm import GLMClient

        async for delta in GLMClient().stream_chat(messages, model=None):
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

    if backend == "claude_code":
        from core.backends.claude_code import ClaudeCodeClient

        # Message-level streaming (probe-confirmed: one whole text block per
        # assistant event, NOT token deltas). Drive stream_chat directly so the
        # UI surfaces CC replies live. Stateless fallback path: no posture /
        # resume (execute_node's state-aware block threads those).
        async for delta in ClaudeCodeClient().stream_chat(
            prompt=_messages_to_prompt(messages),
            resume_session_id=None,
            posture={},
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
            posture={"model": model_override} if model_override else {},
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
            posture={"model": model_override} if model_override else {},
        ):
            yield delta
        return

    if backend == "kimi_cli":
        from core.backends.kimi_cli import KimiCliClient

        async for delta in KimiCliClient().stream_chat(
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
    parts: list[str] = []
    async for delta in _stream_to_backend(messages, backend, model_override):
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
    return "".join(parts)


async def execute_node(state: "AgentState") -> dict:
    """LangGraph node: call the backend and accumulate the response."""
    task = state.get("task", "")
    backend = state.get("backend", "local")
    messages = list(state.get("messages", []))
    approval_response = state.get("approval_response")

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
    if recalled:
        bullets = "\n".join(f"- {f.body.get('text', '')}" for f in recalled[:5])
        messages.insert(0, {"role": "system", "content": f"Prior context:\n{bullets}"})

    # ── Project context splice (server-side projects) ────────────────────────
    # The conversation's project instructions, resolved by the gateway. Inserted
    # at the front so it frames the turn ahead of recalled facts. Reaches every
    # backend: HTTP backends via _stream_and_collect(messages, ...) and the
    # claude_code planner via _messages_to_prompt(messages).
    project_instructions = (state.get("project_instructions") or "").strip()
    if project_instructions:
        messages.insert(0, {"role": "system", "content": f"Project context:\n{project_instructions}"})
    # This is the ONE locale injection point, front-most, covering both the HTTP join
    # and the subprocess _messages_to_prompt (both concatenate role:"system" messages in order).
    # locale_directive_message() is the single source of the guard (None => English, never raises).
    _locale_directive = locale_directive_message(state.get("locale"))
    if _locale_directive is not None:
        messages.insert(0, _locale_directive)

    # ── Opt-in LangChain tool-calling loop (P0) ──────────────────────────────
    # Only fires for a tool-enabled face on a tool-capable backend. Every other
    # face/backend combination falls through to the existing transport below.
    if _is_tool_enabled(state.get("face_id", "assistant"), backend):
        writer = _get_stream_writer()
        user_token = _current_user_id.set(state.get("user_id"))
        conv_token = _current_conversation_id.set(state.get("conversation_id"))
        try:
            response = await _run_tool_loop(
                messages, backend, state.get("face_id", "assistant"), writer
            )
            await _append_agent_turn_event(state, assistant_response=response)
            return {
                "messages": [{"role": "assistant", "content": response}],
                "approval_required": False,
                "approval_response": None,
                "error": None,
            }
        except Exception as exc:
            response_text = f"Execution error: {exc}"
            await _append_agent_turn_event(
                state, assistant_response=response_text, error_msg=str(exc)
            )
            return {
                "messages": [{"role": "assistant", "content": response_text}],
                "error": str(exc),
                "approval_required": False,
            }
        finally:
            _current_user_id.reset(user_token)
            _current_conversation_id.reset(conv_token)

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
        except NoOpenCodeAvailable:
            # No registered instance for this workspace — retarget to the
            # face's escalation_backend and fall through to standard send.
            backend = state.get("escalation_backend") or "kimi_platform"

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
            backend = state.get("escalation_backend") or "claude_api"
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
        except AgyThrottled:
            # Subscription quota spent → fall through to the face's escalation
            # backend (= gemini_pro, the metered REST twin).
            backend = state.get("escalation_backend") or "gemini_pro"
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
                "error": str(exc),
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
        # Honour a UI-picked model on the planner tier: a `switch_model` /
        # model-pin lands in state.model_override (api/server.py → state), but the
        # planner posture only carries a `profile` (e.g. gpt), so without this the
        # pick was silently dropped — you could enter "gpt mode" but never choose
        # WHICH gpt model. The stateless fan-out (worker) path already threads
        # model_override; mirror it here so an explicit pick binds. Combined with
        # codex_code._build_argv, a gpt-profile face runs the chosen gpt model
        # natively (no litellm). Council-shape overrides divert before execute,
        # so a model_override reaching this backend is a genuine model id. str() first:
        # a malformed direct-to-core payload could send a non-string model, and a bare
        # .strip() would crash the node instead of being ignored.
        model_override = str(state.get("model_override") or "").strip()
        if model_override:
            posture = {**posture, "model": model_override}
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
        except CodexThrottled:
            # Provider 429 via litellm → fall through to the face's escalation
            # backend (= opencode_serve: codex above opencode, opencode fallback).
            backend = state.get("escalation_backend") or "opencode_serve"
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
                "error": str(exc),
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
        pin = await _check_escalation_pin(backend)
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
            escalation = state.get("escalation_backend") or "kimi_platform"
            await _pin_escalation("kimi_code", escalation, ttl_seconds=_PIN_TTL_SECONDS)
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
        response_text = f"Execution error: {exc}"
        await _append_agent_turn_event(
            state, assistant_response=response_text, error_msg=str(exc),
        )
        return {
            "messages": [
                {"role": "assistant", "content": response_text}
            ],
            "error": str(exc),
            "approval_required": False,
        }
    except Exception as exc:
        response_text = f"Execution error: {exc}"
        await _append_agent_turn_event(
            state, assistant_response=response_text, error_msg=str(exc),
        )
        return {
            "messages": [
                {"role": "assistant", "content": response_text}
            ],
            "error": str(exc),
            "approval_required": False,
        }
