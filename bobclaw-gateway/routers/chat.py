import asyncio
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from time import perf_counter
from uuid import UUID

import aiohttp
from aiohttp import WSMsgType, web

from app_state import POSTGRES_POOL_KEY, get_conversation_session, get_user_session
from auth import authenticate_ws
from config import config
# core.permissions is on PYTHONPATH (the gateway runs with PYTHONPATH=bobclaw-core);
# auth_routes.py imports Scope the same way. Used to vouch the agent scope to core (P3).
from core.permissions import scope_vouch
from redis_client import get_redis

logger = logging.getLogger(__name__)

router = web.RouteTableDef()


def _agent_scope_fields(token_claims: dict | None, secret: str) -> dict:
    """The ``scope`` + ``scope_vouch`` to forward to core for an AGENT token (P3).

    Returns ``{}`` for a human/admin token or a token with no dict scope — so the
    upstream payload is byte-identical to before for human chats. For an agent token
    carrying a dict scope, returns ``{"scope", "scope_vouch"}`` where the vouch is an
    HMAC the gateway minted with the SHARED ``BOBCLAW_SECRET``. The scope is taken ONLY
    from the verified token claims — NEVER from the client WS frame — so a client cannot
    self-assert a wider blast radius, and core rejects any scope lacking this vouch.
    """
    claims = token_claims or {}
    if claims.get("token_type") != "agent":
        return {}
    token_scope = claims.get("scope")
    if not isinstance(token_scope, dict):
        return {}
    return {"scope": token_scope, "scope_vouch": scope_vouch(token_scope, secret)}


def _is_pin_authoritative(token_claims: dict | None, face_id) -> bool:
    """Headless contract: an AGENT token's explicit face is authoritative — core then
    skips the intent heuristic (which can never select an explicitly-pinned face like
    planner-cc-edit). True only for an agent token WITH a face set; human tokens / no
    face ⇒ False (interactive heuristic unchanged). Changes face routing only — the
    Gate scope (vouched separately) is the security boundary."""
    return (token_claims or {}).get("token_type") == "agent" and bool(face_id)


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


async def _verify_conversation_access(pool, conversation_id: str, user_id: str) -> bool:
    """Return True if the conversation exists and belongs to user_id."""
    if pool is None:
        return True
    row = await pool.fetchrow(
        "SELECT id FROM conversations WHERE id = $1 AND user_id = $2 AND is_archived = FALSE",
        conversation_id,
        user_id,
    )
    return row is not None


async def _persist_approval(
    pool,
    *,
    approval_id_hex: str,
    conversation_id: str,
    user_id: str,
    action_type: str,
    details: dict,
) -> None:
    """Persist an approval row when core emits approval_request.

    Failure-tolerant: logs and returns on any exception so the chat
    stream is never blocked by an audit-trail write failure.
    """
    if pool is None:
        return
    try:
        approval_uuid = UUID(approval_id_hex)
    except ValueError:
        logger.warning("Skipping approval persist — bad approval_id %r", approval_id_hex)
        return
    try:
        await pool.execute(
            """
            INSERT INTO approvals (id, conversation_id, user_id, action_type, details, status)
            VALUES ($1, $2, $3, $4, $5, 'pending')
            ON CONFLICT (id) DO NOTHING
            """,
            approval_uuid,
            UUID(conversation_id) if conversation_id else None,
            user_id,
            action_type,
            json.dumps(details),
        )
    except Exception as exc:
        logger.warning("Approval persist failed for %s: %s", approval_id_hex, exc)


async def _publish_approval(user_id: str, payload: dict) -> None:
    """Publish a pub/sub message to the user's approvals channel.

    Failure-tolerant: logs and returns on any Redis exception so the
    chat stream is never blocked by a notification failure.
    """
    try:
        client = get_redis()
        await client.publish(f"bobclaw:approvals:{user_id}", json.dumps(payload))
    except Exception as exc:
        logger.warning("Redis publish to approvals:%s failed: %s", user_id, exc)


async def _save_message(pool, conversation_id: str, role: str, content: str, metadata=None):
    if pool is None:
        return None
    return await pool.fetchrow(
        """
        INSERT INTO messages (conversation_id, role, content, metadata)
        VALUES ($1, $2, $3, $4)
        RETURNING id, conversation_id, role, content, created_at, metadata
        """,
        conversation_id,
        role,
        content,
        json.dumps(metadata or {}),
    )


def _parse_stream_event(line: str):
    line = line.strip()
    if not line:
        return None
    if line.startswith("data:"):
        line = line[5:].strip()
    try:
        parsed = json.loads(line)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        return {"type": "chunk", "content": line}
    return None


async def _send_ws_error(ws: web.WebSocketResponse, message: str, code: str, close: bool = False) -> None:
    await ws.send_json({"type": "error", "message": message, "code": code})
    if close:
        await ws.close()


async def _get_conversation_history(pool, conversation_id: str, limit: int, max_chars: int) -> list[dict]:
    """Return the last *limit* messages for *conversation_id*, ordered oldest-first.

    Truncates to *max_chars* total content size (char-counted, per-message) to
    guard against oversized payloads from very long conversations.
    """
    if pool is None:
        return []
    rows = await pool.fetch(
        """
        SELECT role, content, created_at
        FROM messages
        WHERE conversation_id = $1
        ORDER BY created_at DESC, id DESC
        LIMIT $2
        """,
        conversation_id,
        limit,
    )
    # rows are newest-first here. Accumulate from the newest backwards so the
    # char budget drops the OLDEST turns, then restore chronological order —
    # recent context is the valuable end.
    kept_newest_first: list[dict] = []
    total_chars = 0
    for row in rows:
        content = row["content"] or ""
        total_chars += len(content)
        if total_chars > max_chars:
            break
        kept_newest_first.append({"role": row["role"], "content": content})
    kept_newest_first.reverse()
    return kept_newest_first


async def _stream_chat_to_client(ws, conversation_id: str, payload: dict, conv_session: dict, pool, user_id: str = "admin", history: list[dict] | None = None, token_claims: dict | None = None) -> None:
    """Stream a chat turn from core to the WebSocket client.

    Runs as a cancellable background task so ``stop_generation`` can interrupt
    the upstream SSE stream mid-flight.

    *token_claims* is the authenticated JWT payload (Neck Beard P3). For an AGENT
    token it carries the Gate ``scope`` we forward to core under an HMAC vouch — the
    scope comes ONLY from the verified token, never from the client ``payload`` frame.
    """
    started = perf_counter()
    assistant_parts: list[str] = []
    completion = None
    superseded = False

    # Pull the conversation's stored pins + its project's instructions so server
    # state is authoritative (default-face/backend inheritance and project
    # context survive even when the client sends nothing). Fail-open if no pool.
    conv_row = None
    if pool is not None:
        conv_row = await pool.fetchrow(
            "SELECT c.face_id, c.model_preference, c.backend_preference, "
            "p.name AS project_name, p.description AS project_description, "
            "p.instructions AS project_instructions "
            "FROM conversations c LEFT JOIN projects p ON c.project_id = p.id "
            "WHERE c.id = $1",
            conversation_id,
        )

    # Compose the project context conveyed to the model. Lead with the project
    # IDENTITY (name) so the assistant knows which chat workspace it's in —
    # explicitly distinct from any code repo it may be running in (claude_code
    # auto-loads the repo's CLAUDE.md, which otherwise dominates "what project
    # are we in?"). Goal/instructions follow.
    project_context = None
    if conv_row and conv_row["project_name"]:
        parts = [
            f'This conversation belongs to the user\'s BoBClaw chat workspace '
            f'(a "project") named "{conv_row["project_name"]}". This is the user\'s '
            f'organizational project for the conversation — NOT the code repository '
            f'you may be running in. If the user asks which project this chat is in, '
            f'the answer is "{conv_row["project_name"]}".'
        ]
        if conv_row["project_description"]:
            parts.append(f'Project goal: {conv_row["project_description"]}')
        if conv_row["project_instructions"]:
            parts.append(f'Project instructions:\n{conv_row["project_instructions"]}')
        project_context = "\n\n".join(parts)

    upstream_payload = {
        "conversation_id": conversation_id,
        "content": payload["content"],
        "face_id": payload.get("face_id") or conv_session.get("face_id") or (conv_row["face_id"] if conv_row else None),
        "model": payload.get("model") or conv_session.get("model") or (conv_row["model_preference"] if conv_row else None),
        "backend": conv_session.get("backend") or (conv_row["backend_preference"] if conv_row else None),
        "profile": payload.get("profile") or conv_session.get("profile"),
        "locale": payload.get("locale") or conv_session.get("locale") or "en",  # absent => "en"
        "project_instructions": project_context,
        "history": history or [],
        "user_id": user_id,
    }

    # Headless contract: an AGENT token's face is an explicit, already-vetted choice
    # (the gateway checked it against the token's `faces` claim) — tell core to honor
    # the pin and SKIP the intent heuristic, which can never select an explicitly-pinned
    # face like planner-cc-edit. Only when a face is actually set; human turns keep the
    # interactive heuristic (byte-identical). Pin only changes face routing, not the
    # Gate scope (still vouched below), so it grants no new capability.
    if _is_pin_authoritative(token_claims, upstream_payload["face_id"]):
        upstream_payload["pin_authoritative"] = True

    # Neck Beard P3 — forward the AGENT TOKEN's vouched Gate scope so core can light up
    # the Gate for destructive sub-actions (no-op for human tokens — byte-identical).
    upstream_payload.update(_agent_scope_fields(token_claims, config.BOBCLAW_SECRET))

    try:
        # A CoCouncil restart turn (two claude_code grounding spawns + an extra
        # panel round) runs well past aiohttp's default 300s total timeout, and a
        # SINGLE grounding/planner spawn is silent on the SSE stream for up to
        # core CC_TIMEOUT_SECONDS (300s). So: no overall cap (total=None) and a
        # per-read cap (sock_read) comfortably larger than the longest silent
        # spawn — that still kills a genuinely-stuck core, but never a slow turn.
        _stream_timeout = aiohttp.ClientTimeout(total=None, sock_read=600)
        async with aiohttp.ClientSession(timeout=_stream_timeout) as session:
            async with session.post(
                f"{config.CORE_URL.rstrip('/')}" + "/api/chat",
                json=upstream_payload,
            ) as response:
                if response.status >= 400:
                    body = await response.text()
                    await _send_ws_error(ws, body or "Upstream chat failed", "upstream_error")
                    return

                async for raw_chunk in response.content:
                    decoded = raw_chunk.decode(errors="ignore")
                    for line in decoded.splitlines():
                        event = _parse_stream_event(line)
                        if not event:
                            continue
                        event_type = event.get("type")
                        if event_type == "chunk":
                            chunk = event.get("content", "")
                            assistant_parts.append(chunk)
                            await ws.send_json(
                                {
                                    "type": "chunk",
                                    "content": chunk,
                                    "model": event.get("model") or upstream_payload.get("model"),
                                    "backend": event.get("backend") or upstream_payload.get("backend"),
                                }
                            )
                        elif event_type == "approval_request":
                            approval_id_hex = event.get("approval_id") or ""
                            action = event.get("action") or "task_approval"
                            details = event.get("details") or {}
                            # Persist for the dashboard tile + audit trail.
                            await _persist_approval(
                                pool,
                                approval_id_hex=approval_id_hex,
                                conversation_id=conversation_id,
                                user_id=user_id,
                                action_type=action,
                                details=details,
                            )
                            # Live notify the user's dashboard subscribers.
                            await _publish_approval(user_id, {
                                "type": "new_approval",
                                "approval_id": approval_id_hex,
                                "conversation_id": conversation_id,
                                "action_type": action,
                                "details": details,
                            })
                            await ws.send_json({
                                "type": "approval_request",
                                "approval_id": approval_id_hex,
                                "action": action,
                                "details": details,
                            })
                        elif event_type == "error":
                            # Upstream (core) emitted a structured error event
                            # (e.g. route_node rejecting an unknown backend as
                            # code=state_error). Forward it so the client sees
                            # the reason instead of an empty completion.
                            await _send_ws_error(
                                ws,
                                str(event.get("message") or "upstream error"),
                                str(event.get("code") or "upstream_error"),
                            )
                        elif event_type == "message_complete":
                            completion = event
    except asyncio.CancelledError:
        if getattr(asyncio.current_task(), "_superseded", False):
            superseded = True
            await ws.send_json({"type": "generation_stopped", "code": "superseded"})
        pass

    assistant_message = "".join(assistant_parts)
    saved = await _save_message(
        pool,
        conversation_id,
        "assistant",
        assistant_message,
        {
            "tokens_in": (completion or {}).get("tokens_in", 0),
            "tokens_out": (completion or {}).get("tokens_out", 0),
        },
    )
    if not superseded:
        elapsed_ms = int((perf_counter() - started) * 1000)
        await ws.send_json(
            {
                "type": "message_complete",
                "message_id": str((completion or {}).get("message_id") or (saved or {}).get("id") or ""),
                "tokens_in": int((completion or {}).get("tokens_in", 0)),
                "tokens_out": int((completion or {}).get("tokens_out", 0)),
                "elapsed_ms": int((completion or {}).get("elapsed_ms", elapsed_ms)),
            }
        )


@router.get("/ws/chat")
async def chat_socket(request: web.Request) -> web.StreamResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    payload, initial_message = await authenticate_ws(request, ws, allow_agent=True)
    if payload is None:
        return ws

    user_id = payload.get("sub", "admin")
    session_state = get_user_session(request.app, user_id)
    pool = request.app[POSTGRES_POOL_KEY]

    def _cleanup_stream_task(task: asyncio.Task) -> None:
        """Called when a stream task finishes (naturally or by cancellation)."""
        session_state.pop("active_stream", None)
        exc = task.exception()
        if exc is not None and not isinstance(exc, asyncio.CancelledError):
            logger.exception("Stream task failed unexpectedly")

    async def _handle_message(data: dict) -> bool:
        message_type = data.get("type")

        if message_type == "switch_face":
            face_id = str(data.get("face_id") or "").strip()
            conversation_id = str(data.get("conversation_id") or "").strip()
            if not conversation_id:
                await _send_ws_error(ws, "conversation_id is required", "invalid_conversation")
                return True
            if not await _verify_conversation_access(pool, conversation_id, user_id):
                await _send_ws_error(ws, "Conversation not found or access denied", "not_found")
                return True
            conv_session = get_conversation_session(request.app, user_id, conversation_id)
            if not face_id:
                # Empty face_id clears the pin → back to "Auto" (unpinned face
                # routing). The UI sends this when the user re-selects Auto.
                conv_session["face_id"] = None
                conv_session["face_name"] = None
                # Persist so the unpin survives a restart AND overrides any face
                # inherited from the conversation's project default.
                if pool is not None:
                    await pool.execute(
                        "UPDATE conversations SET face_id = NULL, updated_at = NOW() WHERE id = $1 AND user_id = $2",
                        conversation_id, user_id,
                    )
                await ws.send_json(
                    {"type": "face_switched", "face_id": None, "face_name": None}
                )
                return True
            conv_session["face_id"] = face_id
            conv_session["face_name"] = data.get("face_name") or face_id
            if pool is not None:
                await pool.execute(
                    "UPDATE conversations SET face_id = $2, updated_at = NOW() WHERE id = $1 AND user_id = $3",
                    conversation_id, face_id, user_id,
                )
            await ws.send_json(
                {
                    "type": "face_switched",
                    "face_id": face_id,
                    "face_name": conv_session["face_name"],
                }
            )
            return True

        if message_type == "switch_profile":
            # Pin a saved profile (HOW layer) to this conversation. Session-only
            # (no DB column yet) — empty clears it. The next turn's upstream payload
            # reads conv_session["profile"], so a council-shaped profile runs.
            profile = str(data.get("profile") or "").strip()
            conversation_id = str(data.get("conversation_id") or "").strip()
            if not conversation_id:
                await _send_ws_error(ws, "conversation_id is required", "invalid_conversation")
                return True
            if not await _verify_conversation_access(pool, conversation_id, user_id):
                await _send_ws_error(ws, "Conversation not found or access denied", "not_found")
                return True
            conv_session = get_conversation_session(request.app, user_id, conversation_id)
            conv_session["profile"] = profile or None
            await ws.send_json({"type": "profile_switched", "profile": profile or None})
            return True

        if message_type == "switch_locale":
            # Pin a locale to this conversation. Session-only (no DB column yet) —
            # empty clears it (defaults to "en" upstream). The next turn's upstream
            # payload reads conv_session["locale"].
            locale = str(data.get("locale") or "").strip()
            conversation_id = str(data.get("conversation_id") or "").strip()
            if not conversation_id:
                await _send_ws_error(ws, "conversation_id is required", "invalid_conversation")
                return True
            if not await _verify_conversation_access(pool, conversation_id, user_id):
                await _send_ws_error(ws, "Conversation not found or access denied", "not_found")
                return True
            conv_session = get_conversation_session(request.app, user_id, conversation_id)
            conv_session["locale"] = locale or None
            await ws.send_json({"type": "locale_switched", "locale": locale or None})
            return True

        if message_type == "switch_model":
            model = str(data.get("model") or "").strip()
            backend = str(data.get("backend") or "").strip()
            conversation_id = str(data.get("conversation_id") or "").strip()
            if not conversation_id:
                await _send_ws_error(ws, "conversation_id is required", "invalid_conversation")
                return True
            if not await _verify_conversation_access(pool, conversation_id, user_id):
                await _send_ws_error(ws, "Conversation not found or access denied", "not_found")
                return True
            conv_session = get_conversation_session(request.app, user_id, conversation_id)
            if not backend:
                # Empty backend clears the pin → back to "Auto" (unpinned
                # backend routing). The UI sends this when re-selecting Auto.
                conv_session["model"] = None
                conv_session["backend"] = None
                # Persist the unpin so it survives a restart AND overrides a
                # backend inherited from the conversation's project default
                # (otherwise the server-side fallback re-applies the stored pin).
                if pool is not None:
                    await pool.execute(
                        "UPDATE conversations SET backend_preference = NULL, model_preference = NULL, updated_at = NOW() WHERE id = $1 AND user_id = $2",
                        conversation_id, user_id,
                    )
                await ws.send_json(
                    {"type": "model_switched", "model": None, "backend": None}
                )
                return True
            conv_session["model"] = model or None
            conv_session["backend"] = backend
            if pool is not None:
                await pool.execute(
                    "UPDATE conversations SET backend_preference = $2, model_preference = $3, updated_at = NOW() WHERE id = $1 AND user_id = $4",
                    conversation_id, backend, (model or None), user_id,
                )
            await ws.send_json({"type": "model_switched", "model": model or None, "backend": backend})
            return True

        if message_type == "message":
            conversation_id = str(data.get("conversation_id") or "").strip()
            content = str(data.get("content") or "").strip()
            if not conversation_id or not content:
                await _send_ws_error(ws, "conversation_id and content are required", "invalid_message")
                return True
            if not await _verify_conversation_access(pool, conversation_id, user_id):
                await _send_ws_error(ws, "Conversation not found or access denied", "not_found")
                return True
            history = await _get_conversation_history(
                pool, conversation_id,
                limit=config.HISTORY_MESSAGE_COUNT,
                max_chars=config.HISTORY_MAX_CHARS,
            )
            await _save_message(pool, conversation_id, "user", content)
            conv_session = get_conversation_session(request.app, user_id, conversation_id)
            if data.get("face_id"):
                conv_session["face_id"] = data["face_id"]
            if data.get("model"):
                conv_session["model"] = data["model"]
            # Cancel any existing stream before starting a new one
            existing = session_state.get("active_stream")
            if existing is not None and not existing.done():
                setattr(existing, "_superseded", True)
                existing.cancel()
            # Launch streaming as a background task so stop_generation can cancel it
            task = asyncio.create_task(
                _stream_chat_to_client(ws, conversation_id, data, conv_session, pool, user_id, history, payload)
            )
            task.add_done_callback(_cleanup_stream_task)
            session_state["active_stream"] = task
            return True

        if message_type == "approval_response":
            # Neck Beard P3 — an agent token must NOT self-approve its own parked
            # destructive action (that would defeat the always-human gate). Only a
            # human/admin token may resume an approval; an agent's destructive actions
            # surface to the human /approvals queue, resolved out-of-band. (This closes
            # the deferred P1/P2 finding: an agent on /ws/chat could otherwise forward
            # an approval_response straight to core /api/chat/approval.)
            if payload.get("token_type") == "agent":
                await _send_ws_error(ws, "Agent tokens cannot submit approvals", "forbidden")
                return True
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        f"{config.CORE_URL.rstrip('/')}" + "/api/chat/approval",
                        json={
                            "approval_id": data.get("approval_id"),
                            "decision": data.get("decision"),
                            "edit_content": data.get("edit_content"),
                        },
                    ) as response:
                        if response.status >= 400:
                            body = await response.text()
                            await _send_ws_error(ws, body or "Approval forwarding failed", "approval_error")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Core unreachable / timeout: surface an error frame instead of
                # letting the exception bubble out of _handle_message and silently
                # kill the WebSocket (the message path guards this; this one didn't).
                logger.warning("approval forwarding to core failed: %s", exc)
                await _send_ws_error(ws, "Approval service unavailable", "approval_error")
            return True

        if message_type == "stop_generation":
            active = session_state.get("active_stream")
            if active is not None and not active.done():
                active.cancel()
                await ws.send_json({"type": "generation_stopped", "code": "stopped"})
            else:
                await ws.send_json({"type": "error", "message": "No active generation to stop", "code": "no_active_generation"})
            return True

        await _send_ws_error(ws, "Unsupported message type", "unsupported_message")
        return True

    if initial_message is not None:
        await _handle_message(initial_message)

    async for incoming in ws:
        if incoming.type == WSMsgType.TEXT:
            try:
                data = json.loads(incoming.data)
            except json.JSONDecodeError:
                await _send_ws_error(ws, "Invalid JSON", "invalid_json")
                continue
            await _handle_message(data)
        elif incoming.type == WSMsgType.ERROR:
            logger.debug("WebSocket closed with exception: %s", ws.exception())
            break
        elif incoming.type in {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED}:
            break

    return ws
