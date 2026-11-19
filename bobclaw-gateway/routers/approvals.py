"""
BoBClaw Gateway — Approvals dashboard endpoints

Surfaces pending approvals from the orchestrator to the dashboard tile.
v1 narrow: only the existing task_requires_approval gate (email/form/
purchase/dangerous shell) flows through here. v2 expands to cost-cap,
fan-out width override, and paid-backend escalation gates.

Endpoints:
  GET  /approvals                  — list user's approvals (default: pending)
  GET  /approvals/{id}             — single approval detail
  POST /approvals/{id}/decide      — record decision + resume the agent turn
  GET  /ws/approvals               — live notifications via Redis pub/sub
"""
import asyncio
import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import aiohttp
from aiohttp import WSMsgType, web
from redis.exceptions import TimeoutError as RedisTimeoutError

from app_state import POSTGRES_POOL_KEY
from auth import authenticate_ws, CredentialWebSocketResponse
from config import config
from redis_client import get_redis

logger = logging.getLogger(__name__)

router = web.RouteTableDef()

_VALID_STATUSES = {"pending", "approved", "rejected", "expired"}
_VALID_DECISIONS = {"approve", "reject"}

# Recognized approval kinds (``action_type`` values) + their display/policy metadata. The approvals
# surface itself stays action_type-AGNOSTIC (any kind still lists/decides through the existing
# endpoints) — this registry only lets a client label a kind and know whether it is a proposal that
# never auto-applies. Additive: adding a kind here lights up its metadata without touching a route.
# MS9-F7 adds ``forest_fork`` (a research-forest fork proposal): proposal-only, always human-decided.
# MS9-F6 adds ``forest_experiment`` (a research-forest experiment above the §7.4 auto-budget OR a
# state-mutating one): proposal-only, always human-decided — mirrors the ``forest_fork`` pattern.
KNOWN_APPROVAL_KINDS: dict[str, dict] = {
    "task_approval": {
        "label": "Task approval",
        "proposal_only": False,
        "requires_human": True,
        "description": "A generic gated task action (email / form / purchase / dangerous shell).",
    },
    "cc_edit": {
        "label": "Code edit",
        "proposal_only": True,
        "requires_human": True,
        "description": "A proposed code diff to review (and optionally edit) before it is applied.",
    },
    "worker_scope_review": {
        "label": "Worker scope review",
        "proposal_only": False,
        "requires_human": True,
        "description": "A fan-out worker's output flagged for scope-drift review.",
    },
    "forest_fork": {
        "label": "Research-forest fork",
        "proposal_only": True,
        "requires_human": True,
        "description": (
            "A proposal to fork a research subtree into its own standing program. Carries a "
            "'why it can't stay a subtree' rationale and a verifiable seed key; nothing is applied "
            "until it is approved."
        ),
    },
    "forest_experiment": {
        "label": "Research-forest experiment",
        "proposal_only": True,
        "requires_human": True,
        "description": (
            "A proposal to run a research-forest experiment that is above the auto-budget "
            "($2/tree/epoch, $5/day forest-wide) OR is state-mutating. Carries the runner, config, "
            "estimated cost, and the budget math; nothing runs until it is approved."
        ),
    },
}


def kind_metadata(action_type: str | None) -> dict:
    """Return the metadata for *action_type*, or a null-safe default for an unknown kind.

    Keeps the surface action_type-agnostic: an unrecognized kind still resolves to a usable record
    (labelled by its raw action_type, treated as human-decided, not proposal-only) so a new core
    kind is served before it is ever registered here.
    """
    meta = KNOWN_APPROVAL_KINDS.get(action_type or "")
    if meta is not None:
        return {"action_type": action_type, **meta}
    return {
        "action_type": action_type,
        "label": action_type or "unknown",
        "proposal_only": False,
        "requires_human": True,
        "description": "",
    }


def _jsonable(value):
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def _record_to_dict(record):
    data = _jsonable(dict(record))
    # Inflate JSONB stored as string back into a dict
    details = data.get("details")
    if isinstance(details, str):
        try:
            data["details"] = json.loads(details)
        except (ValueError, json.JSONDecodeError):
            pass
    return data


def _get_pool(request: web.Request):
    pool = request.app[POSTGRES_POOL_KEY]
    if pool is None:
        raise web.HTTPServiceUnavailable(text='{"error": "Postgres unavailable"}', content_type="application/json")
    return pool


def _get_user_id(request: web.Request) -> str:
    return request.get("user", {}).get("sub", "admin")


# ── Portal agent approval guard ──────────────────────────────────────────────
# The REST middleware's default-deny for agent tokens is SKIPPED while
# BOB_AGENT_INNER_GATES=open (see auth.py:371-380), so in that mode an agent
# bearer can reach this router with no non-agent identity check — while
# /ws/approvals rejects agent tokens unconditionally (auth.authenticate_ws
# default allow_agent=False). The portal therefore refuses decisions until this
# guard exists; the guard makes the agent path explicit, ownership-checked and
# operator-enabled rather than implicitly allowed by an open gate.
#
# Scope: AGENT tokens only. Human tokens keep today's behaviour exactly
# (middleware identity + the atomic owned-and-pending UPDATE below).
_PORTAL_APPROVALS_ENV = "BOB_PORTAL_APPROVALS"
_TRUTHY = {"1", "true", "yes", "on"}


def _portal_approvals_enabled() -> bool:
    """Operator switch for agent-token decisions (read per request).

    Deliberately read from the environment on every call rather than cached at
    import: activation is ``BOB_PORTAL_APPROVALS=1`` + gateway restart, and
    rollback is unsetting it, with no code change either way.
    """
    return os.getenv(_PORTAL_APPROVALS_ENV, "").strip().lower() in _TRUTHY


def _is_agent_request(request: web.Request) -> bool:
    return request.get("user", {}).get("token_type") == "agent"


def _agent_refusal(detail: str) -> web.HTTPForbidden:
    """Typed 403 for an agent-token decision refusal (no DB write)."""
    return web.HTTPForbidden(
        text=json.dumps({"error": detail, "detail": detail}),
        content_type="application/json",
    )


@router.get("/approvals")
async def list_approvals(request: web.Request) -> web.Response:
    limit = max(1, min(int(request.query.get("limit", "50")), 200))
    offset = max(0, int(request.query.get("offset", "0")))
    status_filter = request.query.get("status", "pending")
    if status_filter not in _VALID_STATUSES and status_filter != "all":
        raise web.HTTPBadRequest(text='{"error": "Invalid status"}', content_type="application/json")
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    if status_filter == "all":
        rows = await pool.fetch(
            """
            SELECT id, conversation_id, user_id, action_type, details, status, approved_by, decided_at, created_at
            FROM approvals
            WHERE user_id = $1
            ORDER BY created_at DESC
            LIMIT $2 OFFSET $3
            """,
            user_id, limit, offset,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, conversation_id, user_id, action_type, details, status, approved_by, decided_at, created_at
            FROM approvals
            WHERE user_id = $1 AND status = $2
            ORDER BY created_at DESC
            LIMIT $3 OFFSET $4
            """,
            user_id, status_filter, limit, offset,
        )
    return web.json_response({
        "items": [_record_to_dict(row) for row in rows],
        "limit": limit,
        "offset": offset,
        "status": status_filter,
    })


@router.get("/approvals/digest")
async def approvals_digest(request: web.Request) -> web.Response:
    """Gate-activity digest for the authenticated user (GR-P3-finish).

    Surfaces two slices so Travis can review what the scope Gate did unattended:
      * ``gate_cleared``    — recent rows the Gate auto-cleared (``approved_by='gate'``).
      * ``flagged_pending`` — pending worker_scope_review rows that need a human
        decision (``status='pending'`` AND ``action_type='worker_scope_review'``).
    Each slice is bounded; ``counts`` carries the per-slice length.
    """
    limit = max(1, min(int(request.query.get("limit", "20")), 100))
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    gate_cleared_rows = await pool.fetch(
        """
        SELECT id, conversation_id, user_id, action_type, details, status, approved_by, decided_at, created_at
        FROM approvals
        WHERE user_id = $1 AND approved_by = 'gate'
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id, limit,
    )
    flagged_pending_rows = await pool.fetch(
        """
        SELECT id, conversation_id, user_id, action_type, details, status, approved_by, decided_at, created_at
        FROM approvals
        WHERE user_id = $1 AND status = 'pending' AND action_type = 'worker_scope_review'
        ORDER BY created_at DESC
        LIMIT $2
        """,
        user_id, limit,
    )

    gate_cleared = [_record_to_dict(row) for row in gate_cleared_rows]
    flagged_pending = [_record_to_dict(row) for row in flagged_pending_rows]
    return web.json_response({
        "gate_cleared": gate_cleared,
        "flagged_pending": flagged_pending,
        "counts": {
            "gate_cleared": len(gate_cleared),
            "flagged_pending": len(flagged_pending),
        },
        "limit": limit,
    })


@router.get("/approvals/kinds")
async def list_approval_kinds(request: web.Request) -> web.Response:
    """Serve the recognized approval kinds + their metadata (read-only, static).

    Registered BEFORE ``/approvals/{approval_id}`` so the literal path wins over the id pattern
    (mirrors ``/approvals/digest``). Backs a dashboard that labels a pending item and marks a
    proposal-only kind (e.g. ``forest_fork``) as one that never auto-applies. Needs no Postgres, so
    it stays available during a DB outage.
    """
    kinds = [kind_metadata(k) for k in sorted(KNOWN_APPROVAL_KINDS)]
    return web.json_response({"kinds": kinds})


@router.get("/approvals/{approval_id}")
async def get_approval(request: web.Request) -> web.Response:
    approval_id = request.match_info["approval_id"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    row = await pool.fetchrow(
        """
        SELECT id, conversation_id, user_id, action_type, details, status, approved_by, decided_at, created_at
        FROM approvals
        WHERE id = $1 AND user_id = $2
        """,
        approval_id, user_id,
    )
    if row is None:
        raise web.HTTPNotFound()
    return web.json_response(_record_to_dict(row))


RESUME_DELIVERY_MAX_S = 600.0  # matches chat.py's silent-spawn ceiling (sock_read=600)


_RESUME_TASKS: set = set()


def _spawn_resume_delivery(coro) -> None:
    """Keep a strong reference to a background delivery task and log its failure.

    Without the reference CPython may collect a pending task mid-flight, and without
    the done-callback an exception inside it disappears (no row, no log, no signal).
    """
    task = asyncio.create_task(coro)
    _RESUME_TASKS.add(task)

    def _done(t: asyncio.Task) -> None:
        _RESUME_TASKS.discard(t)
        if not t.cancelled():
            exc = t.exception()
            if exc is not None:
                logger.warning("Resume delivery task failed: %s", exc)

    task.add_done_callback(_done)


async def _persist_resume_output(
    response, pool, *, conversation_id, user_id, approval_id_hex, decision="approve"
) -> dict:
    """Drain the resumed turn's SSE and persist its answer into the conversation.

    Why this exists: the REST decide path has no WebSocket connection to hand core's
    resumed stream to, so before this helper the approved turn ran (and was billed)
    while its answer was never stored — the decision was visible, the effect was not
    (verified 2026-10-24: two provider completions and a completed T0 projection,
    conversation still holding only the user row).

    Best-effort by contract: any failure is reported in the returned dict and logged,
    never as a failed decision. A row with no conversation is still drained (so core
    is not abandoned mid-stream) and reported as ``discarded_no_conversation``.
    """
    content = getattr(response, "content", None)
    if content is None or pool is None:
        return {"status": "not_applicable"}
    # Local imports: chat.py is a sibling router and must not load at module import.
    from routers.chat import (
        _parse_stream_event,
        _persist_approval,
        _publish_approval,
        _save_message,
    )

    # asyncpg hands back a uuid.UUID for a UUID column; the chat helpers and every
    # published payload expect the string form (json.dumps cannot serialise UUID).
    conv_id = str(conversation_id) if conversation_id else None

    parts: list[str] = []
    completion = None
    pending_approval = None
    fault = None
    buffer = b""

    def _handle(event: dict) -> None:
        nonlocal completion, pending_approval, fault
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "chunk":
            parts.append(event.get("content") or "")
        elif kind == "message_complete":
            completion = event
        elif kind == "approval_request":
            pending_approval = event
        elif kind == "error":
            fault = str(event.get("message") or "upstream error")

    # SSE frames straddle network reads: buffer to a line boundary before parsing,
    # otherwise a split frame is stored as raw JSON or a message_complete is missed.
    # Only `data:` lines are protocol events; anything else (`event:`, comments, keep-
    # alives) would otherwise be stored as assistant text by the lenient parser.
    async for raw in content:
        buffer += raw if isinstance(raw, (bytes, bytearray)) else str(raw).encode()
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            if line.strip().lower().startswith(b"data:"):
                _handle(_parse_stream_event(line.decode(errors="ignore")))
    if buffer.strip().lower().startswith(b"data:"):
        _handle(_parse_stream_event(buffer.decode(errors="ignore")))

    if pending_approval is not None:
        # The resumed turn paused again on a further gated step: record that approval
        # through the same helpers the chat path uses, so it is not lost with the stream.
        approval_hex = pending_approval.get("approval_id") or ""
        details = pending_approval.get("details") or {}
        action = pending_approval.get("action") or "task_approval"
        # Keep the text already produced before the new gate, as the WS chat path does.
        if parts and conv_id:
            await _save_message(
                pool, conv_id, "assistant", "".join(parts),
                {"resumed_from_approval": approval_id_hex, "partial": True},
            )
        await _persist_approval(
            pool,
            approval_id_hex=approval_hex,
            conversation_id=conv_id,
            user_id=user_id,
            action_type=action,
            details=details,
        )
        await _publish_approval(user_id, {
            "type": "new_approval",
            "approval_id": approval_hex,
            "conversation_id": conv_id,
            "action_type": action,
            "details": details,
            "resumed_from_approval": approval_id_hex,
        })
        return {"status": "awaiting_approval", "approval_id": approval_hex}

    if decision == "reject" and completion is not None and not parts:
        # A rejection completes with a system message and no assistant output; core
        # still emits message_complete for it, so judge by the missing content only.
        return {"status": "no_output_expected"}
    if completion is None or not parts:
        return {"status": "error" if fault else "incomplete",
                "detail": (fault or "")[:300], "chunks": len(parts)}
    if not conv_id:
        return {"status": "discarded_no_conversation", "chars": len("".join(parts))}
    text = "".join(parts)
    saved = await _save_message(
        pool,
        conv_id,
        "assistant",
        text,
        {
            "tokens_in": (completion or {}).get("tokens_in", 0),
            "tokens_out": (completion or {}).get("tokens_out", 0),
            "resumed_from_approval": approval_id_hex,
        },
    )
    return {
        "status": "persisted",
        "message_id": str((saved or {}).get("id") or ""),
        "chars": len(text),
        "user_id": user_id,
    }


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(request: web.Request) -> web.Response:
    approval_id = request.match_info["approval_id"]
    body_json = await request.json()
    decision = (body_json.get("decision") or "").strip().lower()
    if decision not in _VALID_DECISIONS:
        raise web.HTTPBadRequest(
            text='{"error": "decision must be approve or reject"}',
            content_type="application/json",
        )
    # Optional human-edited content (C4 cc_edit: Travis tweaks the diff before
    # approving). Forwarded verbatim to core so the edited version is applied.
    edit_content = body_json.get("edit_content")
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    # ── Agent-token guard: evaluated BEFORE any DB status write. ─────────────
    # An agent bearer is allowed to decide ONLY when the operator has enabled
    # portal approvals, the row belongs to the token's own sub, and the row is
    # still pending. Every refusal is typed, 403, and leaves the row untouched.
    # Human tokens skip this block entirely (unchanged behaviour).
    deciding_principal = user_id
    if _is_agent_request(request):
        if not _portal_approvals_enabled():
            raise _agent_refusal("agent_approval_disabled")
        # Ownership/state are checked on a read, so a refused decision can be
        # distinguished from a missing row without touching it. The UPDATE
        # below remains the atomic writer (still guarded on pending+owner), so
        # a decision that races this read still cannot double-write.
        current = await pool.fetchrow(
            "SELECT id, user_id, status FROM approvals WHERE id = $1",
            approval_id,
        )
        if current is None:
            raise web.HTTPNotFound()
        if current["user_id"] != user_id:
            raise _agent_refusal("agent_approval_foreign")
        if current["status"] != "pending":
            raise _agent_refusal("agent_approval_stale")
        deciding_principal = f"agent:{user_id}"

    # Atomically update only if pending and owned. Returns the row on success.
    row = await pool.fetchrow(
        """
        UPDATE approvals
        SET status = $2, decided_at = NOW(), approved_by = $4
        WHERE id = $1 AND user_id = $3 AND status = 'pending'
        RETURNING id, conversation_id, user_id, action_type, details, status, approved_by, decided_at, created_at
        """,
        approval_id,
        "approved" if decision == "approve" else "rejected",
        user_id,
        deciding_principal,
    )
    if row is None:
        # Either not found, not owned, or already decided
        raise web.HTTPNotFound()

    # Proxy to core /api/chat/approval to resume the agent turn.
    # core uses the 32-char hex form of the approval_id as resume token.
    try:
        approval_id_hex = UUID(approval_id).hex
    except ValueError:
        approval_id_hex = approval_id  # already hex form
    core_payload = {"approval_id": approval_id_hex, "decision": decision}
    if isinstance(edit_content, str) and edit_content.strip():
        core_payload["edit_content"] = edit_content
    core_url = f"{config.CORE_URL.rstrip('/')}" + "/api/chat/approval"
    # Probe acceptance inline (headers only, the pre-existing fast path), then hand the
    # response stream to a background task: the resume's output has no WebSocket to ride
    # on, and the decide reply must not wait for a long approved turn.
    try:
        session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(
                total=None, connect=10, sock_connect=10,
                sock_read=RESUME_DELIVERY_MAX_S,
            )
        )
        try:
            response = await session.post(core_url, json=core_payload)
        except Exception:
            await session.close()
            raise
        if response.status >= 400:
            try:
                body = await response.text()
            except Exception as exc:  # noqa: BLE001 — core died between headers and body
                await session.close()
                logger.warning(
                    "Core /api/chat/approval status %d with unreadable body for %s: %s",
                    response.status, approval_id_hex, exc,
                )
                return web.json_response({
                    **_record_to_dict(row),
                    "decision": "recorded",
                    "agent_resume": "failed",
                    "agent_resume_message": str(exc),
                    "resume_delivery": "not_attempted",
                })
            await session.close()
            logger.warning(
                "Core /api/chat/approval returned %d for %s: %s",
                response.status, approval_id_hex, body,
            )
            # Row is already updated; return success with a warning so
            # the dashboard still reflects the user's decision. The
            # agent-side replay can be retried out-of-band if needed.
            return web.json_response({
                **_record_to_dict(row),
                "decision": "recorded",
                "agent_resume": "failed",
                "agent_resume_message": body,
                "resume_delivery": "not_attempted",
            })
        _spawn_resume_delivery(
            _drain_resume_response(
                session,
                response,
                pool=pool,
                conversation_id=row["conversation_id"],
                user_id=user_id,
                approval_id_hex=approval_id_hex,
                decision=decision,
            )
        )
    except Exception as exc:
        logger.warning("Failed to proxy decision to core: %s", exc)
        return web.json_response({
            **_record_to_dict(row),
            "decision": "recorded",
            "agent_resume": "failed",
            "agent_resume_message": str(exc),
            "resume_delivery": "not_attempted",
        })

    return web.json_response({
        **_record_to_dict(row),
        "decision": "recorded",
        "agent_resume": "ok",
        "resume_delivery": "scheduled",
    })


async def _drain_resume_response(session, response, *, pool, conversation_id,
                                 user_id, approval_id_hex, decision) -> None:
    """Own an already-open resume response: persist its output, then close the session."""
    try:
        try:
            result = await _persist_resume_output(
                response,
                pool,
                conversation_id=conversation_id,
                user_id=user_id,
                approval_id_hex=approval_id_hex,
                decision=decision,
            )
        except Exception as exc:  # noqa: BLE001 — the decision is already recorded
            logger.warning("Resume delivery for %s failed: %s", approval_id_hex, exc)
            result = {"status": "failed", "detail": str(exc)[:200]}
        logger.info("Approval resume delivery for %s: %s", approval_id_hex, result.get("status"))
        # The decide reply cannot wait for this outcome; publish it on the approvals
        # channel the dashboard already subscribes to so the effect is observable.
        try:
            from routers.chat import _publish_approval
            await _publish_approval(user_id, {
                "type": "approval_resume",
                "approval_id": approval_id_hex,
                "conversation_id": str(conversation_id) if conversation_id else None,
                "status": result.get("status"),
                "message_id": result.get("message_id"),
            })
        except Exception:  # noqa: BLE001 — notification is best-effort
            pass
    finally:
        await session.close()


@router.get("/ws/approvals")
async def approvals_socket(request: web.Request) -> web.StreamResponse:
    """Subscribe to live approval notifications for the authenticated user.

    Auth pattern mirrors /ws/chat: HTTP Authorization header OR first
    JSON message {"type": "auth", "token": "..."}.
    """
    ws = CredentialWebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    payload, _ = await authenticate_ws(request, ws, allow_agent=False)
    if payload is None:
        return ws

    user_id = payload.get("sub", "admin")
    channel = f"bobclaw:approvals:{user_id}"

    pubsub = None
    try:
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(channel)
    except Exception as exc:
        await ws.send_json({"type": "error", "message": f"redis unavailable: {exc}", "code": "redis_unavailable"})
        await ws.close()
        return ws

    async def _forward_redis_to_ws() -> None:
        # redis-py 8.x pubsub reads carry a ~5s socket read-timeout, so a blocking
        # ``async for message in pubsub.listen()`` RAISES ``TimeoutError`` on the first
        # idle gap and kills the forward loop — a watcher then never sees approvals
        # published after it went idle. Poll with ``get_message`` (returns None on an
        # idle tick; a raised read-timeout is swallowed) so the loop survives. Mirrors
        # the /ws/monitor fix; see fix(flight) in this branch.
        try:
            while True:
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                except (asyncio.TimeoutError, RedisTimeoutError):
                    continue
                if message is None:
                    continue
                if message.get("type") != "message":
                    continue
                data = message.get("data")
                if isinstance(data, bytes):
                    data = data.decode(errors="ignore")
                try:
                    parsed = json.loads(data) if isinstance(data, str) else data
                except (ValueError, json.JSONDecodeError):
                    parsed = {"type": "raw", "data": data}
                await ws.send_json(parsed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("approvals WS forward failed for %s: %s", user_id, exc)

    forward_task = asyncio.create_task(_forward_redis_to_ws())
    try:
        async for incoming in ws:
            # Client may send pings or close — we don't expect commands here.
            if incoming.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break
    finally:
        forward_task.cancel()
        try:
            await forward_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()
        except Exception:
            pass
    return ws


