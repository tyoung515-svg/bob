"""
BoBClaw Core — aiohttp app factory and REST handlers

This module wires the core's public HTTP surface:

    GET  /health                  — liveness probe
    GET  /api/faces               — lightweight face summaries
    GET  /api/faces/{face_id}     — full Face profile
    GET  /api/models/local        — discovered local model backends
    POST /api/chat                — SSE streaming chat turn (B1b)
    POST /api/chat/approval       — resume after approval (B1c)

State is injected via :func:`build_app` so tests can construct an
application with stubs.  ``start.py`` is the production entry point
that supplies the real ``FaceRegistry``, ``LocalModelRouter``, compiled
LangGraph, and asyncpg pool.
"""
from __future__ import annotations

import json
import logging
import shutil
import uuid
from time import perf_counter
from typing import Any, Optional

import asyncpg
from aiohttp import web
from langgraph.types import Command

from api.ws_protocol import (
    approval_request_event,
    chunk_event,
    error_event,
    message_complete_event,
)
from core.backends.local_router import LocalModelRouter
from core.config import GRAPH_RECURSION_LIMIT, config
from core import teams, team_proposer
from core.faces.registry import FaceRegistry
from core.memory.bootstrap import get_memory
from core.memory.exceptions import L1ValidationFailed, MemoryConfigError
from core.memory.write_fence import WriteFenceViolation

logger = logging.getLogger(__name__)

_DISCONNECT_ERRORS = (ConnectionResetError,)

# Cloud backends wired into execute_node, in display order. Each tuple is
# (backend name, config API-key attr, config default-model attr). The
# ``available`` flag in /api/models/available is key-present for these — a
# backend with no API key configured is wired but not usable.
_CLOUD_BACKENDS: tuple[tuple[str, str, str], ...] = (
    ("claude_api", "ANTHROPIC_API_KEY", "ANTHROPIC_MODEL"),
    ("deepseek_v4_flash", "DEEPSEEK_API_KEY", "DEEPSEEK_MODEL"),
    ("kimi_code", "KIMI_API_KEY", "KIMI_MODEL"),
    ("kimi_platform", "MOONSHOT_API_KEY", "KIMI_PLATFORM_MODEL"),
    ("minimax", "MINIMAX_API_KEY", "MINIMAX_MODEL"),
    ("gemini_flash", "GOOGLE_API_KEY", "GEMINI_FLASH_MODEL"),
    ("gemini_pro", "GOOGLE_API_KEY", "GEMINI_PRO_MODEL"),
    ("gemini_deep_research", "GOOGLE_API_KEY", "GEMINI_DEEP_RESEARCH_MODEL"),
)


async def _safe_write(
    response: web.StreamResponse,
    data: bytes,
    conversation_id: str,
) -> bool:
    """Write SSE data to the stream; return False if the client disconnected."""
    try:
        await response.write(data)
        return True
    except _DISCONNECT_ERRORS:
        logger.info(
            "client disconnected mid-stream, conversation=%s", conversation_id,
        )
        return False


# ─── Typed app-state keys (suppresses NotAppKeyWarning) ───────────────────────
FACES_KEY: web.AppKey[FaceRegistry] = web.AppKey("faces", FaceRegistry)
ROUTER_KEY: web.AppKey[LocalModelRouter] = web.AppKey("router", LocalModelRouter)
POOL_KEY: web.AppKey[asyncpg.Pool] = web.AppKey("pg_pool", asyncpg.Pool)
GRAPH_KEY: web.AppKey[Any] = web.AppKey("graph", object)

# approval_id -> thread_id.  An approval request is identified by a fresh
# UUID; /api/chat/approval (B1c) will look up the thread_id to resume the
# corresponding LangGraph execution.  Process-local for now.
APPROVALS_KEY: web.AppKey[dict] = web.AppKey("approvals", dict)


routes = web.RouteTableDef()


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _sse_line(obj: dict) -> bytes:
    """Render a dict as a single ``data: ...\\n\\n`` SSE frame (utf-8)."""
    return f"data: {json.dumps(obj)}\n\n".encode("utf-8")


def _rough_tokens(text: str) -> int:
    """Rough char→token estimate (~4 chars per token).

    Used for response metadata until a proper tokenizer is wired in.
    """
    return max(1, len(text) // 4) if text else 0


# ─── Handlers ─────────────────────────────────────────────────────────────────

@routes.get("/health")
async def health(_: web.Request) -> web.Response:
    """Liveness probe plus observable memory write-fence degradation state."""
    payload = {"status": "ok"}
    if config.MEMORY_ENABLED:
        try:
            mem = get_memory()
        except MemoryConfigError:
            pass
        else:
            fence = getattr(mem, "write_fence", None)
            degraded = bool(getattr(fence, "degraded", False))
            payload["memory_write_fence_degraded"] = degraded
            payload["memory_write_fence"] = {
                "writes_refused": degraded,
                "resource": getattr(fence, "resource_identity", None),
            }
            if degraded:
                payload["memory_write_fence"]["reason"] = getattr(
                    fence, "degraded_reason", "write lock is held by another writer"
                )
    return web.json_response(payload)


@routes.get("/api/faces")
async def list_faces(request: web.Request) -> web.Response:
    """Return compact summaries of every configured Face."""
    registry = request.app[FACES_KEY]
    return web.json_response([s.model_dump() for s in registry.list_faces()])


@routes.get("/api/faces/{face_id}")
async def get_face(request: web.Request) -> web.Response:
    """Return the full Face profile, or 404 if unknown."""
    registry = request.app[FACES_KEY]
    face_id = request.match_info["face_id"]
    try:
        face = registry.get_face(face_id)
    except KeyError:
        return web.json_response(
            error_event(f"Unknown face_id: {face_id}", code="face_not_found"),
            status=404,
        )
    return web.json_response(face.model_dump())


# ─── /api/routing-view — JOAT v0 read surface ─────────────────────────────────

async def _build_routing_view(registry: FaceRegistry, team: Optional[str] = None) -> dict:
    """Compute the live faces → roles → resolved-backends map under *team* (or the
    process-active team when None) by calling ``teams.resolve`` per face.

    Read-only; no mutation. ``team`` previews a specific fleet; None reflects the
    process default (BOBCLAW_TEAM env, else per-face).
    """
    from core.backends._lc_openai import TOOL_CAPABLE_BACKENDS

    rows = []
    for face in registry.all_faces():
        resolved = await teams.resolve(
            face.role, face=face, want_tools=bool(face.allowed_tools), team=team
        )
        rows.append(
            {
                "id": face.id,
                "role": face.role,
                "preferred_backend": face.preferred_backend,
                "resolved_backend": resolved,
                "escalation_chain": teams.escalation_chain(face.role, face=face, team=team),
                "tool_capable": resolved in TOOL_CAPABLE_BACKENDS,
            }
        )
    return {
        "active_team": team or teams.get_active_team(),
        "teams": teams.known_teams(),
        # JOAT v1: True once start.py has installed the live health-walk probe — then
        # resolved_backend reflects per-backend health_check()/throttle-pin state and
        # walks the escalation chain. Under the no-op default (tests / un-wired) it is
        # the DECLARED team mapping, so a reader doesn't trust it as health-checked.
        "live_probe": teams.health_probe_is_live(),
        "faces": rows,
    }


def _render_routing_table(view: dict) -> str:
    """Plain-text table of the routing view — the JOAT v0 'text view'."""
    _live = bool(view.get("live_probe", False))
    _note = ("resolved = health-checked, walks escalation chain" if _live
             else "resolved = declared mapping, not health-checked")
    out = [
        f"active_team: {view['active_team'] or '(default - per-face)'}",
        f"teams:       {', '.join(view['teams'])}",
        f"live_probe:  {str(_live).lower()} ({_note})",
        "",
        f"{'FACE':<22} {'ROLE':<8} {'RESOLVED':<18} {'ESCALATION':<28} TOOLS",
        "-" * 86,
    ]
    for f in view["faces"]:
        out.append(
            f"{f['id']:<22} {(f['role'] or '-'):<8} {f['resolved_backend']:<18} "
            f"{' -> '.join(f['escalation_chain']):<28} {'yes' if f['tool_capable'] else ''}"
        )
    return "\n".join(out) + "\n"


@routes.get("/api/routing-view")
async def routing_view(request: web.Request) -> web.Response:
    """The live faces→roles→resolved-backends map + active team (JOAT v0).

    Read-only. ``?team=<name>`` previews a fleet without changing the process env;
    ``?format=text`` returns a plain-text table instead of JSON.
    """
    registry = request.app[FACES_KEY]
    team_q = request.query.get("team") or None
    if team_q is not None and team_q not in teams.known_teams():
        return web.json_response(
            error_event(
                f"Unknown team: {team_q}; known: {teams.known_teams()}",
                code="unknown_team",
            ),
            status=400,
        )
    view = await _build_routing_view(registry, team=team_q)
    if request.query.get("format") == "text":
        return web.Response(text=_render_routing_table(view), content_type="text/plain")
    return web.json_response(view)


# ─── /api/teams — JOAT team store (list / create / delete) ─────────────────────

@routes.get("/api/teams")
async def list_teams_endpoint(request: web.Request) -> web.Response:
    """All selectable teams (built-in + custom) with their role→backend config."""
    return web.json_response({"items": teams.list_teams()})


@routes.post("/api/teams")
async def create_team_endpoint(request: web.Request) -> web.Response:
    """Create a custom team from ``{name, roles:{role:{backend, escalation_chain}}}``.

    Validation errors (bad name, built-in collision, unknown backend/role,
    duplicate) → 400; built-ins are never mutated.
    """
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            error_event("invalid JSON body", code="invalid_json"), status=400
        )
    try:
        created = teams.create_team(
            (body or {}).get("name") or "",
            (body or {}).get("roles") or {},
            overwrite=bool((body or {}).get("overwrite")),
        )
    except ValueError as exc:
        return web.json_response(error_event(str(exc), code="invalid_team"), status=400)
    return web.json_response(created, status=201)


@routes.delete("/api/teams/{name}")
async def delete_team_endpoint(request: web.Request) -> web.Response:
    """Delete a custom team. Built-ins → 400; unknown custom → 404."""
    name = request.match_info["name"]
    try:
        removed = teams.delete_team(name)
    except ValueError as exc:
        return web.json_response(error_event(str(exc), code="invalid_team"), status=400)
    if not removed:
        return web.json_response(
            error_event(f"team {name!r} not found", code="not_found"), status=404
        )
    return web.json_response({"status": "deleted", "name": name})


@routes.get("/api/backends")
async def list_backends_endpoint(request: web.Request) -> web.Response:
    """The backend palette for the team-builder: every registered backend with its
    per-worker USD cost cap + max fan-out width, plus the role vocabulary."""
    from core.config import MAX_FANOUT_WIDTH_BY_BACKEND, MAX_WORKER_USD_BY_BACKEND
    items = [
        {
            "backend": b,
            "max_usd_per_worker": MAX_WORKER_USD_BY_BACKEND[b],
            "max_fanout_width": MAX_FANOUT_WIDTH_BY_BACKEND.get(b),
        }
        for b in sorted(MAX_WORKER_USD_BY_BACKEND)
    ]
    return web.json_response({"items": items, "roles": list(teams.ROLES)})


@routes.post("/api/teams/propose")
async def propose_team_endpoint(request: web.Request) -> web.Response:
    """Ask the team-builder assistant for a JOAT team proposal for a free-text goal.
    Returns ``{goal, name, roles, raw}`` (validated to known backends); never
    persists — the UI fills the builder form for review + Save."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    goal = (body or {}).get("goal") or ""
    proposal = await team_proposer.propose_team(goal)
    return web.json_response(proposal)


@routes.post("/api/teams/refine")
async def refine_team_endpoint(request: web.Request) -> web.Response:
    """One team-builder refine turn: ``{message, history?, draft?}`` → ``{reply, draft,
    raw}``. Multi-turn (the client threads history + draft each round); never persists."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    out = await team_proposer.refine_team(
        (body or {}).get("message") or "",
        history=(body or {}).get("history") or [],
        draft=(body or {}).get("draft"),
    )
    return web.json_response(out)


# ─── /api/profiles — full profile envelopes (superset of teams) ────────────────

@routes.get("/api/profiles")
async def list_profiles_endpoint(request: web.Request) -> web.Response:
    """All profiles (built-in + custom) as full envelopes (roles + role_prompts +
    shape + bounds + schedule)."""
    return web.json_response({"items": teams.list_profiles()})


@routes.get("/api/profiles/{name}")
async def get_profile_endpoint(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    env = teams.load_profile(name)
    if env is None:
        return web.json_response(
            error_event(f"profile {name!r} not found", code="not_found"), status=404
        )
    return web.json_response(env)


@routes.post("/api/profiles")
async def create_profile_endpoint(request: web.Request) -> web.Response:
    """Create a profile from ``{name, roles, shape?, synth_backend?, protocol_bounds?,
    seats?, schedule?}``. Validation errors → 400."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response(error_event("invalid JSON body", code="invalid_json"), status=400)
    name = (body or {}).get("name") or ""
    profile = {k: v for k, v in (body or {}).items() if k not in ("name", "overwrite")}
    try:
        created = teams.create_profile(name, profile, overwrite=bool((body or {}).get("overwrite")))
    except ValueError as exc:
        return web.json_response(error_event(str(exc), code="invalid_profile"), status=400)
    return web.json_response(created, status=201)


@routes.delete("/api/profiles/{name}")
async def delete_profile_endpoint(request: web.Request) -> web.Response:
    name = request.match_info["name"]
    try:
        removed = teams.delete_team(name)  # same YAML store
    except ValueError as exc:
        return web.json_response(error_event(str(exc), code="invalid_profile"), status=400)
    if not removed:
        return web.json_response(
            error_event(f"profile {name!r} not found", code="not_found"), status=404
        )
    return web.json_response({"status": "deleted", "name": name})


@routes.get("/api/models/local")
async def list_local_models(request: web.Request) -> web.Response:
    """Probe Ollama and LM Studio; return reachable backends with models."""
    router = request.app[ROUTER_KEY]
    backends = await router.discover()
    return web.json_response(
        [
            {"name": b.name, "url": b.url, "models": b.models}
            for b in backends
        ]
    )


@routes.get("/api/models/available")
async def list_available_models(request: web.Request) -> web.Response:
    """Return every wired backend with an ``available`` flag.

    Drives the UI's pin selector (PLAN.md T3): the frontend offers "Auto"
    (unpinned, default) plus a pick from these. ``available`` reflects whether
    the backend can actually serve a turn right now:

    * ``local`` — at least one reachable Ollama/LM Studio backend
      (``router.discover()``); ``models`` lists what those backends expose.
    * cloud backends — the API key is configured (key-present); ``model`` is
      the configured default model id for that backend.
    * ``opencode_serve`` — at least one OpenCode instance is configured.

    A backend that is wired but unconfigured is returned with
    ``available: false`` so the UI can show (and disable) it.
    """
    router = request.app[ROUTER_KEY]
    try:
        local_backends = await router.discover()
    except Exception:
        logger.warning("local backend discovery failed", exc_info=True)
        local_backends = []
    local_models = sorted(
        {m for b in local_backends for m in (b.models or [])}
    )

    result: list[dict] = [
        {
            "backend": "local",
            "available": bool(local_backends),
            "model": None,
            "models": local_models,
        }
    ]
    for name, key_attr, model_attr in _CLOUD_BACKENDS:
        result.append(
            {
                "backend": name,
                "available": bool(getattr(config, key_attr, "")),
                "model": getattr(config, model_attr, None),
            }
        )
    # claude_code (planning tier) — the subscription CLI, NOT key-gated: it runs
    # the genuine ``claude`` binary under the user's OAuth login. Available when
    # the binary resolves on PATH (or CC_CLI_PATH points at it). which() only,
    # no spawn — this endpoint is polled by the UI status strip.
    result.append(
        {
            "backend": "claude_code",
            "available": bool(shutil.which(config.CC_CLI_PATH or "claude")),
            "model": None,
        }
    )
    # agy_code (Gemini Second Voice) — subscription CLI, NOT key-gated. AGY_CLI_PATH
    # is an absolute path (agy is not on PATH); which() echoes an absolute exe back.
    result.append(
        {
            "backend": "agy_code",
            "available": bool(shutil.which(config.AGY_CLI_PATH or "agy")),
            "model": None,
        }
    )
    result.append(
        {
            "backend": "opencode_serve",
            "available": bool(config.opencode_instances_parsed()),
            "model": None,
        }
    )
    return web.json_response(result)


# ─── Memory facts: list + forget (T4) ─────────────────────────────────────────

# The L1 extractor stamps this generation_method on every auto-extracted fact
# (core/memory/extractor.py). The memory browser lists only these — not
# manually-seeded or markdown-ingested facts.
_L1_GENERATION_METHOD = "extract_facts_from_event"


def _fact_to_summary(fact) -> dict:
    """Project a Fact into the browser's row shape (text/subject/predicate out
    of body; confidence flattened to a jsonable dict)."""
    from dataclasses import asdict

    body = fact.body or {}
    return {
        "fact_id": fact.fact_id,
        "text": body.get("text"),
        "subject": body.get("subject"),
        "predicate": body.get("predicate"),
        "ts": fact.ts,
        "source_event_id": fact.source_event_id,
        "confidence": asdict(fact.confidence),
    }


def _memory_write_locked_response(memory: Any, exc: WriteFenceViolation) -> web.Response:
    """Return the stable HTTP surface for a degraded family write fence."""
    fence = getattr(memory, "write_fence", None)
    payload = error_event(str(exc), code="memory_write_locked")
    payload["reason"] = getattr(fence, "degraded_reason", "")
    return web.json_response(payload, status=423)


@routes.get("/api/memory/facts")
async def list_memory_facts(request: web.Request) -> web.Response:
    """List L1 (auto-extracted) facts for the memory browser, newest-first.

    Query: ``limit`` (default 50, capped 500), ``offset`` (default 0). Returns
    ``[]`` when memory is disabled or not yet bootstrapped — never 500s.
    """
    if not config.MEMORY_ENABLED:
        return web.json_response([])
    try:
        mem = get_memory()
    except MemoryConfigError:
        return web.json_response([])

    try:
        limit = int(request.query.get("limit", "50"))
        offset = int(request.query.get("offset", "0"))
    except ValueError:
        return web.json_response(
            error_event("limit and offset must be integers", code="invalid_request"),
            status=400,
        )
    limit = max(0, min(limit, 500))
    offset = max(0, offset)

    facts = await mem.fact_store.query(
        {"generation_method": _L1_GENERATION_METHOD}
    )
    facts.sort(key=lambda f: f.ts or "", reverse=True)  # newest-first
    page = facts[offset : offset + limit]
    return web.json_response([_fact_to_summary(f) for f in page])


@routes.delete("/api/memory/facts/{fact_id}")
async def forget_memory_fact(request: web.Request) -> web.Response:
    """Forget a fact: remove it from BOTH the Qdrant vector store and the
    SQLite FactStore.

    Vector-first, then SQLite: if the SQLite delete fails after the vector is
    gone, the fact is simply un-retrievable (it still lists, retry works). The
    reverse order would leave a dangling vector whose fact lookup throws
    ``L1ValidationFailed`` and aborts recall (the bug this guards against —
    see also the recall fail-open in retriever.search).
    """
    fact_id = request.match_info["fact_id"]
    if not config.MEMORY_ENABLED:
        return web.json_response(
            error_event("memory is disabled", code="memory_unavailable"),
            status=503,
        )
    try:
        mem = get_memory()
    except MemoryConfigError:
        return web.json_response(
            error_event("memory not initialised", code="memory_unavailable"),
            status=503,
        )

    try:
        await mem.fact_store.get(fact_id)
    except L1ValidationFailed:
        return web.json_response(
            error_event(f"Unknown fact_id: {fact_id}", code="fact_not_found"),
            status=404,
        )

    try:
        # 1) Qdrant vector(s) — scroll by source_fact_id payload → delete points.
        await mem.indexer.drop_facts([fact_id])
        # 2) SQLite row.
        await mem.fact_store.delete(fact_id)
    except WriteFenceViolation as exc:
        return _memory_write_locked_response(mem, exc)
    return web.json_response({"status": "forgotten", "fact_id": fact_id})


# ─── SSE streamer (shared by /api/chat and /api/chat/approval) ────────────────

async def _stream_graph_turn(
    *,
    request: web.Request,
    graph: Any,
    graph_input: Any,
    thread_id: str,
    approvals: dict,
    face_id: str,
    user_content: str,
    model_override: Optional[str],
    backend_override: Optional[str],
) -> web.StreamResponse:
    """Run a graph turn and stream SSE events back to the client.

    ``graph_input`` is either a fresh initial AgentState (``/api/chat``)
    or a :class:`langgraph.types.Command` (``/api/chat/approval``) that
    resumes an interrupted checkpointed thread.
    """
    # recursion_limit must cover the deepest council loop (a debate of up to
    # COUNCIL_MAX_ROUNDS_CEILING rounds, grounded restarts, fan-out waves) — the
    # langgraph default of 25 would crash a long debate mid-run. See config.
    config = {"configurable": {"thread_id": thread_id},
              "recursion_limit": GRAPH_RECURSION_LIMIT}

    response = web.StreamResponse(
        status=200,
        headers={
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    await response.prepare(request)

    started = perf_counter()
    full_response: list[str] = []
    approval_emitted = False
    disconnected = False

    try:
        # stream_mode is a list → each item is a (mode, payload) tuple.
        #   "custom"  — per-token deltas emitted by execute_node's stream writer
        #   "updates" — per-node state deltas (errors, approvals, and the
        #               non-streaming fan-out/decompose assistant output)
        async for mode, chunk in graph.astream(
            graph_input, config, stream_mode=["updates", "custom"]
        ):
            if disconnected:
                break

            # ── Per-token deltas from execute_node's stream writer ───────────
            if mode == "custom":
                if isinstance(chunk, dict) and chunk.get("type") == "token":
                    delta = chunk.get("content") or ""
                    if delta:
                        full_response.append(delta)
                        if not await _safe_write(
                            response,
                            _sse_line(
                                chunk_event(
                                    content=delta,
                                    model=chunk.get("model") or model_override,
                                    backend=chunk.get("backend") or backend_override,
                                )
                            ),
                            thread_id,
                        ):
                            disconnected = True
                continue

            # ── Per-node state updates ───────────────────────────────────────
            if not isinstance(chunk, dict):
                continue
            for node_name, update in chunk.items():
                if not isinstance(update, dict):
                    continue

                for msg in update.get("messages") or []:
                    if (
                        isinstance(msg, dict)
                        and msg.get("role") == "assistant"
                        and msg.get("content")
                    ):
                        # execute_node already streamed this text token-by-token
                        # via the "custom" channel — don't re-emit it (and don't
                        # double-count it toward the completion token total).
                        # The council nodes (synthesize / council) emit their
                        # whole-block answer the same way (message-level "custom"
                        # chunk via get_stream_writer), so suppress them here too
                        # or the answer double-emits (the 4f7d8f4 streaming-drop
                        # class, inverted). The answer still rides "messages" into
                        # checkpoint state for these nodes.
                        if node_name in ("execute", "synthesize", "council", "ground",
                                         "debate_converge"):
                            continue
                        text = msg["content"]
                        full_response.append(text)
                        if not await _safe_write(
                            response,
                            _sse_line(
                                chunk_event(
                                    content=text,
                                    model=model_override,
                                    backend=backend_override,
                                )
                            ),
                            thread_id,
                        ):
                            disconnected = True
                            break

                if disconnected:
                    break

                if update.get("error"):
                    if not await _safe_write(
                        response,
                        _sse_line(
                            error_event(
                                str(update["error"]),
                                code="state_error",
                            )
                        ),
                        thread_id,
                    ):
                        disconnected = True
                        break

                if update.get("approval_required") and not approval_emitted:
                    approval_id = uuid.uuid4().hex
                    approvals[approval_id] = thread_id
                    approval_emitted = True
                    # A node may supply its own action_type + details (C4
                    # cc_edit); otherwise fall back to the generic task gate.
                    action_type = update.get("approval_action_type") or "task_approval"
                    node_details = update.get("approval_details")
                    details = node_details if isinstance(node_details, dict) else {
                        "face_id": face_id,
                        "task": user_content[:200],
                        "thread_id": thread_id,
                    }
                    if not await _safe_write(
                        response,
                        _sse_line(
                            approval_request_event(
                                approval_id=approval_id,
                                action=action_type,
                                details=details,
                            )
                        ),
                        thread_id,
                    ):
                        disconnected = True
                        break
    except _DISCONNECT_ERRORS:
        logger.info(
            "client disconnected mid-stream, conversation=%s", thread_id,
        )
        disconnected = True
    except Exception as exc:
        logger.exception("chat stream failed")
        try:
            await response.write(
                _sse_line(error_event(f"stream error: {exc}", code="stream_error"))
            )
        except ConnectionResetError:
            pass

    if disconnected:
        try:
            await response.write_eof()
        except _DISCONNECT_ERRORS:
            pass
        return response

    assistant_text = "".join(full_response)
    elapsed_ms = int((perf_counter() - started) * 1000)

    if not approval_emitted:
        try:
            await response.write(
                _sse_line(
                    message_complete_event(
                        message_id=uuid.uuid4().hex,
                        tokens_in=_rough_tokens(user_content),
                        tokens_out=_rough_tokens(assistant_text),
                        elapsed_ms=elapsed_ms,
                        model=model_override,
                        backend=backend_override,
                    )
                )
            )
        except ConnectionResetError:
            pass

    try:
        await response.write_eof()
    except _DISCONNECT_ERRORS:
        pass
    return response


# ─── Scope ingress (Neck Beard P3) ────────────────────────────────────────────

def _resolve_ingress_scope(
    scope: Optional[dict], vouch: Optional[str], secret: str
) -> Optional[dict]:
    """Decide the AgentState scope for a turn from the (scope, vouch) the gateway sent.

    Core has no auth of its own, so a scope is honored ONLY when the gateway has
    attested it with a valid HMAC vouch (``permissions.verify_scope``). This is the
    self-grant guard: a direct-to-core caller (``:7825``) can put a fat ``scope`` in the
    payload but cannot forge ``scope_vouch`` without ``BOBCLAW_SECRET``, so its scope is
    stripped and every destructive sub-action falls back to human (``evaluate_action``
    with ``scope=None`` → ``"human"``).

    Semantics (kept distinct on purpose):
      * absent / ``None`` scope            → ``None`` (legacy: destructive → human).
      * valid-vouched ``{}`` empty scope   → ``{}`` (all-gate: nothing in auto_actions,
                                             destructive → gate/critic, never silent-auto).
      * valid-vouched populated scope      → the scope dict (the Gate honors it).
      * present scope, BAD/absent vouch    → ``None`` + a loud warning (strip, fail closed).
      * malformed scope (vouch can't match)→ ``None`` (verify_scope fails closed).
    """
    if scope is None:
        return None  # legacy turn — unchanged behaviour
    from core.permissions import verify_scope

    if verify_scope(scope, vouch or "", secret):
        # Audit: this turn carries a gateway-attested blast radius. INFO so an operator
        # can see which scoped agent turns ran (and confirm ingress is working live).
        logger.info(
            "scope ingress: honoring a gateway-vouched scope "
            "(auto_actions=%s may_touch=%s)",
            scope.get("auto_actions"), scope.get("may_touch"),
        )
        return scope
    # Present-but-unvouched: the self-grant attack (or a BOBCLAW_SECRET mismatch). Strip
    # to None so destructive sub-actions fail closed to human; never honor it. Loud so a
    # misconfig is visible (vs. silently degrading every agent action).
    logger.warning(
        "scope ingress: rejecting an un-vouched scope claim on /api/chat "
        "(self-grant blocked / BOBCLAW_SECRET mismatch); scope stripped, "
        "destructive actions fall back to human. vouch_present=%s secret_set=%s",
        bool(vouch), bool(secret),
    )
    return None


# ─── /api/chat — SSE streaming turn (B1b) ─────────────────────────────────────

@routes.post("/api/chat")
async def chat(request: web.Request) -> web.StreamResponse:
    """Stream a single LangGraph turn to the client as Server-Sent Events.

    Body (JSON)::

        {
          "conversation_id": "<uuid>",
          "content":         "<user prompt>",
          "face_id":         "builder-bob",      (optional, defaults to "assistant")
          "model":           "gemma-4-27b",      (optional override)
          "backend":         "local"             (optional override)
        }

    SSE events emitted (each as ``data: <json>\\n\\n``):

    * ``chunk``             — one assistant token/delta
    * ``approval_request``  — graph paused awaiting human sign-off
    * ``message_complete``  — terminal event with token counts + elapsed_ms
    * ``error``             — something failed mid-stream
    """
    graph = request.app.get(GRAPH_KEY)
    if graph is None:
        return web.json_response(
            error_event("LangGraph is not initialised", code="graph_unavailable"),
            status=503,
        )

    faces: FaceRegistry = request.app[FACES_KEY]
    approvals: dict = request.app.setdefault(APPROVALS_KEY, {})

    # ── Parse + validate body ───────────────────────────────────────────────
    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response(
            error_event("Request body must be valid JSON", code="invalid_json"),
            status=400,
        )
    conversation_id = (payload.get("conversation_id") or "").strip()
    content = (payload.get("content") or "").strip()
    if not conversation_id or not content:
        return web.json_response(
            error_event(
                "conversation_id and content are required",
                code="invalid_request",
            ),
            status=400,
        )
    face_id = (payload.get("face_id") or "assistant").strip()
    model_override = payload.get("model")
    backend_override = payload.get("backend")
    # JOAT v0: optional per-conversation team pin (role→backend fleet). None/absent
    # ⇒ the process default (BOBCLAW_TEAM env, else per-face) — no behaviour change.
    team_pin = (payload.get("team") or None)
    # Profiles (HOW layer): optional per-conversation profile name. route_node loads
    # it; a council-shaped profile drives the council subgraph. None/absent ⇒ no change.
    profile_pin = (payload.get("profile") or None)
    locale = payload.get("locale") or "en"  # absent/"en" => no directive => byte-identical.
    history: list[dict] = payload.get("history") or []
    # Project-level instructions resolved by the gateway (LEFT JOIN conversation
    # -> project). Spliced into the system prompt by execute_node. None/absent
    # when the conversation has no project.
    project_instructions = payload.get("project_instructions") or None
    # Gateway-derived user identity (JWT subject), threaded to tool contextvars.
    user_id = payload.get("user_id")
    # Headless contract: the gateway sets this for an agent-token turn whose face was
    # explicitly chosen (and checked against the token's `faces` claim) — route_node
    # then honors the pin and skips the intent heuristic. Changes face routing only
    # (the Gate scope above is the security boundary), so it is safe to pass plainly.
    pin_authoritative = bool(payload.get("pin_authoritative"))
    # NB-W2 A2 — hierarchical-managers trigger. Like pin_authoritative this only selects
    # a TOPOLOGY (recall → manager_dispatch, the 2-level agent tree); it grants no new
    # capability and the Gate/scope below remains the security boundary, so it is safe to
    # pass plainly. A profile with `hierarchical: true` sets the same flag in route_node.
    # F8: require a real JSON `true` — `bool()` would cast the strings "false"/"0" (and any
    # truthy junk) to True. Mirrors the strict bool the profile path validates in teams.py.
    hierarchical = payload.get("hierarchical") is True
    # Neck Beard P3 — scope ingress. The gateway forwards the agent token's Gate scope
    # plus an HMAC vouch it minted with the shared BOBCLAW_SECRET. Honor the scope ONLY
    # when the vouch validates (else strip → destructive sub-actions fall back to human).
    # A direct-to-core caller cannot forge the vouch, so it cannot self-grant auto_actions.
    resolved_scope = _resolve_ingress_scope(
        payload.get("scope"), payload.get("scope_vouch"), config.BOBCLAW_SECRET
    )

    # ── Build initial AgentState ─────────────────────────────────────────────
    try:
        tools_allowed = faces.get_allowed_tools(face_id)
    except KeyError:
        tools_allowed = []

    initial_messages: list[dict] = []
    if history:
        formatted = "\n".join(
            f"{m['role']}: {m['content']}" for m in history
        )
        initial_messages.append(
            {"role": "system", "content": f"Prior conversation:\n{formatted}"}
        )

    initial_state = {
        "messages": initial_messages,
        "task": content,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "face_id": face_id,
        "model_override": model_override,
        "backend_override": backend_override,
        "team": team_pin,
        "profile_name": profile_pin,
        "locale": locale,
        "pin_authoritative": pin_authoritative,
        "hierarchical": hierarchical,
        "backend": backend_override or "local",
        "tools_allowed": tools_allowed,
        "approval_required": False,
        "approval_response": None,
        "artifacts": [],
        "error": None,
        "project_instructions": project_instructions,
        # P3: only set when a valid gateway-vouched scope rode in (else None ⇒ today's
        # human-gated behaviour). The Gate (execute/dispatch/worker) reads state["scope"].
        "scope": resolved_scope,
    }
    thread_id = f"{conversation_id}:{uuid.uuid4().hex[:8]}"

    return await _stream_graph_turn(
        request=request,
        graph=graph,
        graph_input=initial_state,
        thread_id=thread_id,
        approvals=approvals,
        face_id=face_id,
        user_content=content,
        model_override=model_override,
        backend_override=backend_override,
    )


# ─── /api/chat/approval — resume interrupted graph (B1c) ──────────────────────

_VALID_DECISIONS = {"approve", "reject"}


@routes.post("/api/chat/approval")
async def approval(request: web.Request) -> web.StreamResponse:
    """Resume a paused LangGraph turn with the user's approval decision.

    Body (JSON)::

        {
          "approval_id": "<hex from prior approval_request event>",
          "decision":    "approve" | "reject"
        }

    On success the response is an SSE stream identical in shape to
    ``/api/chat``.  On an unknown ``approval_id`` a 404 JSON error is
    returned instead of an SSE stream; on a malformed body, a 400.
    """
    graph = request.app.get(GRAPH_KEY)
    if graph is None:
        return web.json_response(
            error_event("LangGraph is not initialised", code="graph_unavailable"),
            status=503,
        )

    approvals: dict = request.app.setdefault(APPROVALS_KEY, {})

    try:
        payload = await request.json()
    except (json.JSONDecodeError, ValueError):
        return web.json_response(
            error_event("Request body must be valid JSON", code="invalid_json"),
            status=400,
        )

    approval_id = (payload.get("approval_id") or "").strip()
    decision = (payload.get("decision") or "").strip().lower()
    # Optional human-edited diff for a cc_edit approval (C4). Threaded into
    # state so execute_node applies the edited version on approve.
    edit_content = payload.get("edit_content")

    if not approval_id or decision not in _VALID_DECISIONS:
        return web.json_response(
            error_event(
                "approval_id and decision ('approve'|'reject') are required",
                code="invalid_request",
            ),
            status=400,
        )

    # Single-use: pop the mapping so the same approval_id can't be replayed.
    thread_id = approvals.pop(approval_id, None)
    if thread_id is None:
        return web.json_response(
            error_event(
                f"Unknown or already-consumed approval_id: {approval_id}",
                code="approval_not_found",
            ),
            status=404,
        )

    # Resume the paused turn.  The graph is compiled with
    # interrupt_before=["approval"], so ``Command(update=...)`` writes the
    # decision into the checkpointed state AND leaves the "next=(approval,)"
    # step armed; the graph then runs approval → execute to completion.
    # ``aupdate_state`` alone would clear ``next`` and cause the stream to
    # exit immediately without producing any assistant output.
    resume_update: dict = {"approval_response": decision}
    if isinstance(edit_content, str) and edit_content.strip():
        resume_update["approval_edit_content"] = edit_content
    return await _stream_graph_turn(
        request=request,
        graph=graph,
        graph_input=Command(update=resume_update),
        thread_id=thread_id,
        approvals=approvals,
        face_id="",
        user_content="",
        model_override=None,
        backend_override=None,
    )


# ─── App factory ──────────────────────────────────────────────────────────────

def build_app(
    *,
    faces: Optional[FaceRegistry] = None,
    router: Optional[LocalModelRouter] = None,
    graph: Any = None,
    pg_pool: Optional[asyncpg.Pool] = None,
) -> web.Application:
    """Construct the aiohttp Application with routes and typed app state.

    All dependencies are injectable so tests can supply stubs.  When
    ``faces``/``router`` are omitted, real instances are constructed.
    ``graph`` and ``pg_pool`` may be omitted here and attached by
    ``start.py``'s ``_on_startup`` (both are built asynchronously).
    """
    app = web.Application()
    app[FACES_KEY] = faces if faces is not None else FaceRegistry()
    app[ROUTER_KEY] = router if router is not None else LocalModelRouter()
    app[APPROVALS_KEY] = {}
    if graph is not None:
        app[GRAPH_KEY] = graph
    if pg_pool is not None:
        app[POOL_KEY] = pg_pool
    app.add_routes(routes)
    return app
