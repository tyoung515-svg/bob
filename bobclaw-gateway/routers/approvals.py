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
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

import aiohttp
from aiohttp import WSMsgType, web

from app_state import POSTGRES_POOL_KEY
from auth import authenticate_ws
from config import config
from redis_client import get_redis

logger = logging.getLogger(__name__)

router = web.RouteTableDef()

_VALID_STATUSES = {"pending", "approved", "rejected", "expired"}
_VALID_DECISIONS = {"approve", "reject"}


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

    Surfaces two slices so the operator can review what the scope Gate did unattended:
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
    # Optional human-edited content (C4 cc_edit: the operator tweaks the diff before
    # approving). Forwarded verbatim to core so the edited version is applied.
    edit_content = body_json.get("edit_content")
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    # Atomically update only if pending and owned. Returns the row on success.
    row = await pool.fetchrow(
        """
        UPDATE approvals
        SET status = $2, decided_at = NOW()
        WHERE id = $1 AND user_id = $3 AND status = 'pending'
        RETURNING id, conversation_id, user_id, action_type, details, status, approved_by, decided_at, created_at
        """,
        approval_id,
        "approved" if decision == "approve" else "rejected",
        user_id,
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
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{config.CORE_URL.rstrip('/')}" + "/api/chat/approval",
                json=core_payload,
            ) as response:
                if response.status >= 400:
                    body = await response.text()
                    logger.warning(
                        "Core /api/chat/approval returned %d for %s: %s",
                        response.status, approval_id_hex, body,
                    )
                    # Row is already updated; return success with a warning so
                    # the dashboard still reflects the user's decision. The
                    # agent-side replay can be retried out-of-band if needed.
                    return web.json_response({
                        **_record_to_dict(row),
                        "agent_resume": "failed",
                        "agent_resume_message": body,
                    })
    except Exception as exc:
        logger.warning("Failed to proxy decision to core: %s", exc)
        return web.json_response({
            **_record_to_dict(row),
            "agent_resume": "failed",
            "agent_resume_message": str(exc),
        })

    return web.json_response({
        **_record_to_dict(row),
        "agent_resume": "ok",
    })


@router.get("/ws/approvals")
async def approvals_socket(request: web.Request) -> web.StreamResponse:
    """Subscribe to live approval notifications for the authenticated user.

    Auth pattern mirrors /ws/chat: HTTP Authorization header OR first
    JSON message {"type": "auth", "token": "..."}.
    """
    ws = web.WebSocketResponse(heartbeat=30)
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
        try:
            async for message in pubsub.listen():
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


