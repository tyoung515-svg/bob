import asyncio
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from time import perf_counter
from uuid import UUID, uuid4

import aiohttp
from aiohttp import WSMsgType, web

from app_state import POSTGRES_POOL_KEY, get_conversation_session, get_user_session
from auth import authenticate_ws, CredentialWebSocketResponse
from config import config
# core.permissions is on PYTHONPATH (the gateway runs with PYTHONPATH=bobclaw-core);
# auth_routes.py imports Scope the same way. Used to vouch the agent scope to core (P3).
from core.permissions import scope_vouch
from redis_client import get_redis

logger = logging.getLogger(__name__)

router = web.RouteTableDef()

# MS9-W1 — the U7/U8 council theater frames. Core emits these on the chat SSE stream ONLY
# when the turn opted into emit_events (the app's Council theater start-turn); the relay
# below forwards them VERBATIM so the app's Live view renders the real seat/round stream.
# A chat turn that never opts in receives none of these ⇒ the relay is byte-identical, and
# a client that ignores unknown types is unaffected (chat-safe).
_COUNCIL_FRAME_TYPES = frozenset({"council_event", "council_seat", "council_synth"})


def _agent_scope_fields(token_claims: dict | None, secret: str) -> dict:
    """The ``scope`` + ``scope_vouch`` to forward for agent and portal tokens.

    Returns ``{}`` for a human/admin token or a token with no dict scope — so the
    upstream payload is byte-identical to before for human chats. For an agent token
    carrying a dict scope, returns ``{"scope", "scope_vouch"}`` where the vouch is an
    HMAC the gateway minted with the SHARED ``BOBCLAW_SECRET``. The scope is taken ONLY
    from the verified token claims — NEVER from the client WS frame — so a client cannot
    self-assert a wider blast radius, and core rejects any scope lacking this vouch.
    """
    claims = token_claims or {}
    if claims.get("token_type") not in {"agent", "portal"}:
        return {}
    token_scope = claims.get("scope")
    if not isinstance(token_scope, dict):
        return {}
    return {"scope": token_scope, "scope_vouch": scope_vouch(token_scope, secret)}


def _page_context_field(frame: dict | None) -> dict:
    """The additive ``page_context`` to forward to core for the U5 Ask-Bob helper bubble.

    Chat WS → core forward contract (truth table — what the gateway adds to the upstream
    ``/api/chat`` payload, and when):

    ============================  ====================================  =======================
    client ``message`` frame      condition                             forwarded to core
    ============================  ====================================  =======================
    (no ``page_context`` key)     absent                                nothing (byte-identical)
    ``page_context: null``        not a dict                            nothing (byte-identical)
    ``page_context: {}``          empty dict                            nothing (byte-identical)
    ``page_context: {"page":…}``  non-empty dict                        ``{"page_context": {…}}``
    ============================  ====================================  =======================

    So a page_context-free chat turn (every existing client + the main chat screen) forwards a
    BYTE-IDENTICAL upstream payload — the helper bubble is the only sender. Core additionally
    flag-gates the splice (``PAGE_CONTEXT_ENABLED``), so even a forwarded page_context changes
    nothing in the assembled prompt until the flag is on (U5 accept #1). The field carries only
    screen context, never capability — unlike ``scope`` it needs no vouch.
    """
    page_context = (frame or {}).get("page_context")
    if isinstance(page_context, dict) and page_context:
        return {"page_context": page_context}
    return {}


def _emit_events_field(frame: dict | None) -> dict:
    """The additive ``emit_events`` opt-in to forward to core (MS9-W1 live council theater).

    Returns ``{"emit_events": True}`` ONLY when the client ``message`` frame set
    ``emit_events`` to a real JSON ``true`` (the app's Council-theater start-turn) — else
    ``{}`` so every ordinary chat turn forwards a BYTE-IDENTICAL upstream payload. Strict
    ``is True`` (mirrors core's parse) so a truthy string can't flip it. Core then stamps
    ``council_spec["emit_events"]`` and streams the council_* frames the relay forwards.
    """
    if (frame or {}).get("emit_events") is True:
        return {"emit_events": True}
    return {}


def _research_field(frame: dict | None) -> dict:
    """The additive ``research`` opt-in to forward to core (MS2-R2 research orchestrator entry).

    Returns ``{"research": True}`` ONLY when the client ``message`` frame set ``research`` to a
    real JSON ``true`` — else ``{}`` so every ordinary chat turn forwards a BYTE-IDENTICAL
    upstream payload. Strict ``is True`` (mirrors core's parse and ``_emit_events_field``). Core
    sets ``research_request`` and the turn enters the research lane — which reads the federated
    LKS instances (e.g. hermes-corpus) when ``RESEARCH_LKS_INSTANCES`` is configured there.
    """
    if (frame or {}).get("research") is True:
        return {"research": True}
    return {}


def _is_pin_authoritative(token_claims: dict | None, face_id) -> bool:
    """Headless contract: an AGENT token's explicit face is authoritative — core then
    skips the intent heuristic (which can never select an explicitly-pinned face like
    planner-cc-edit). True only for an agent token WITH a face set; human tokens / no
    face ⇒ False (interactive heuristic unchanged). Changes face routing only — the
    Gate scope (vouched separately) is the security boundary."""
    return (token_claims or {}).get("token_type") == "agent" and bool(face_id)


def _face_denied_for_agent(token_claims: dict | None, face_id) -> bool:
    """Enforce non-empty agent face lists; human/legacy empty lists stay unrestricted."""
    # PATCH-LATER: household-trust "open" mode skips
    # the inner face gate entirely so Bob can work all its positions as it did
    # pre-family-layer. Re-enable strict after the scope-authority migration.
    if config.AGENT_INNER_GATES == "open":
        return False
    claims = token_claims or {}
    if claims.get("token_type") != "agent":
        return False
    faces = claims.get("faces")
    if faces is None:
        return False  # absent/null legacy claim
    # Match the mint endpoint's supported list-of-strings shape and size cap.
    # Invalid claims must never be interpreted as an unlimited token.
    if not isinstance(faces, list) or len(faces) > 32 or not all(isinstance(f, str) for f in faces):
        return True
    if not faces:
        return False
    return (face_id or "assistant") not in faces


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


async def _send_ws_error(ws: web.WebSocketResponse, message: str, code: str, close: bool = False,
                         *, reason: str | None = None) -> None:
    await ws.send_json({"type": "error", "message": message, "code": code,
                        **({"reason": reason} if reason else {})})
    if close:
        await ws.close()


async def _get_conversation_history(pool, conversation_id: str, limit: int, max_chars: int) -> list[dict]:
    """Return the last *limit* messages for *conversation_id*, ordered oldest-first.

    *limit* / *max_chars* are transport ceilings; core fits the result to the routed model's context
    budget. Oldest turns are dropped first once *max_chars* is exceeded, but the newest message is
    never dropped: if it alone exceeds *max_chars* it is truncated in place (it used to return [],
    silently discarding all history). Each item carries ``id`` / ``created_at`` when the row has them.
    """
    if pool is None:
        return []
    rows = await pool.fetch(
        """
        SELECT id, role, content, created_at
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
        item = {"role": row["role"], "content": content}
        try:
            if row["id"] is not None:
                item["id"] = str(row["id"])
        except (KeyError, IndexError):
            pass
        try:
            created_at = row["created_at"]
            if created_at is not None:
                item["created_at"] = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
        except (KeyError, IndexError):
            pass
        if not kept_newest_first and len(content) > max_chars:
            head = max_chars * 2 // 5
            tail = max_chars - head
            item["content"] = (
                content[:head]
                + f"\n[... {len(content) - max_chars} characters omitted from this message to fit the history ceiling ...]\n"
                + content[len(content) - tail:]
            )
            kept_newest_first.append(item)
            total_chars = max_chars
            continue
        total_chars += len(content)
        if total_chars > max_chars:
            break
        kept_newest_first.append(item)
    kept_newest_first.reverse()
    return kept_newest_first


_INDEX_TASKS: set[asyncio.Task] = set()


async def _index_stored_conversation(pool, conversation_id, user_id):
    """Replay only this just-used, owned conversation after the reply is sent.

    There is no reliable lane marker on legacy shared conversations. The fresh
    gateway turn establishes lane membership; it does not authorize a bulk scan.
    Keyset pages avoid the prompt history's message and character ceilings.
    """
    offset, previous_id, after_time, after_id = 0, '', None, None
    try:
        from core.context.ingest import PAGE_SIZE, INLINE_ROW_BYTES, signature
        from core.execution.api import lane_id
        import time

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            while True:
                rows = await pool.fetch('''SELECT m.id, m.role, m.content, m.created_at
                    FROM messages m JOIN conversations c ON c.id=m.conversation_id
                    WHERE c.id=$1 AND c.user_id=$2 AND c.is_archived=FALSE
                      AND ($3::timestamptz IS NULL OR (m.created_at,m.id)>($3,$4::uuid))
                    ORDER BY m.created_at,m.id LIMIT $5''',
                    conversation_id, user_id, after_time, after_id, PAGE_SIZE, timeout=5)
                if not rows:
                    return
                wire_rows = [_jsonable(dict(row)) for row in rows]
                for row in wire_rows:
                    if len(json.dumps(row).encode('utf-8')) > INLINE_ROW_BYTES:
                        row['content'] = None
                now = int(time.time())
                envelope = dict(lane=lane_id(), user_id=user_id, conversation_id=conversation_id,
                                rows=wire_rows, offset=offset,
                                previous_id=previous_id, iat=now, exp=now + 30)
                async with session.post(f"{config.CORE_URL.rstrip('/')}/api/chat/index",
                        json={'envelope': envelope, 'vouch': signature(envelope, config.BOBCLAW_SECRET)}) as response:
                    if response.status != 200:
                        raise RuntimeError(f'Core rejected conversation index page ({response.status})')
                offset += len(rows)
                previous_id = str(rows[-1]['id'])
                after_time, after_id = rows[-1]['created_at'], rows[-1]['id']
                if len(rows) < PAGE_SIZE:
                    return
    except Exception:
        logger.warning('Stored conversation indexing failed for %s; the next turn will replay it',
                       conversation_id, exc_info=True)


async def _stream_chat_to_client(ws, conversation_id: str, payload: dict, conv_session: dict, pool, user_id: str = "admin", history: list[dict] | None = None, token_claims: dict | None = None, vetted_face_id: str | None = None) -> None:
    try:
        await _relay_chat_to_client(ws, conversation_id, payload, conv_session, pool,
                                   user_id, history, token_claims, vetted_face_id)
    finally:
        # Independent of the socket/producer lifetime, and never awaited on the
        # owner's reply path. This also runs after upstream/preflight failures.
        if pool is not None:
            task = asyncio.create_task(_index_stored_conversation(pool, conversation_id, user_id))
            _INDEX_TASKS.add(task)
            task.add_done_callback(_INDEX_TASKS.discard)


async def _relay_chat_to_client(ws, conversation_id: str, payload: dict, conv_session: dict, pool, user_id: str = "admin", history: list[dict] | None = None, token_claims: dict | None = None, vetted_face_id: str | None = None) -> None:
    """Stream a chat turn from core to the WebSocket client.

    Runs as a cancellable background task so ``stop_generation`` can interrupt
    the upstream SSE stream mid-flight.

    *token_claims* is the authenticated JWT payload (Neck Beard P3). For an AGENT
    token it carries the Gate ``scope`` we forward to core under an HMAC vouch — the
    scope comes ONLY from the verified token, never from the client ``payload`` frame.
    """
    started = perf_counter()
    assistant_parts: list[str] = []
    pre_question_saved = None
    pre_question_message_ids: list[str] = []
    request_id = payload.get("request_id")
    turn_metadata = {"request_id": request_id} if isinstance(request_id, str) and request_id else {}
    completion = None
    failure = None
    superseded = False

    # Pull the conversation's stored pins + its project's instructions so server
    # state is authoritative (default-face/backend inheritance and project
    # context survive even when the client sends nothing). Fail-open if no pool.
    conv_row = None
    if pool is not None:
        conv_row = await pool.fetchrow(
            "SELECT c.face_id, c.model_preference, c.backend_preference, c.profile, c.locale, "
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
        "face_id": vetted_face_id or payload.get("face_id") or conv_session.get("face_id") or (conv_row["face_id"] if conv_row else None),
        "model": payload.get("model") or conv_session.get("model") or (conv_row["model_preference"] if conv_row else None),
        "backend": payload.get("backend") or conv_session.get("backend") or (conv_row["backend_preference"] if conv_row else None),
        "profile": payload.get("profile") or conv_session.get("profile") or (conv_row["profile"] if conv_row else None),
        "locale": payload.get("locale") or conv_session.get("locale") or (conv_row["locale"] if conv_row else None) or "en",  # absent => "en"
        "project_instructions": project_context,
        "history": history or [],
        "user_id": user_id,
        **turn_metadata,
    }

    # Revalidate stored pins too: the offered list may have changed since selection.
    if upstream_payload.get('backend') and upstream_payload.get('model'):
        from .models import validate_chat_pin
        try:
            await validate_chat_pin(upstream_payload['backend'], upstream_payload['model'])
        except ValueError as exc:
            await _send_ws_error(ws, str(exc), 'invalid_pin')
            return

    # Scoped Auto uses core's existing default, never the first allowed face.
    # Vouch that concrete choice so core cannot heuristic-swap it outside the list.
    claims = token_claims or {}
    # Only interactive owner chat can offer more steps. Never copy this from a
    # client frame; outside-agent/MCP turns retain their existing round stop.
    upstream_payload['step_questions_allowed'] = claims.get('token_type', 'human') in {'human', 'portal'}
    if not upstream_payload["face_id"] and claims.get("token_type") == "agent" and claims.get("faces"):
        upstream_payload["face_id"] = "assistant"

    # Recheck the actual upstream face after the DB await. Scoped socket turns
    # use the preflight snapshot, not mutable pins reread after side effects.
    if _face_denied_for_agent(token_claims, upstream_payload["face_id"]):
        await _send_ws_error(ws, "Face is not in this token's allow-list", "face_forbidden")
        return

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
    # Work authority is derived only from this socket's authenticated caller.
    # Client frames cannot supply it, and owner-session rights never enter chat.
    from routers.execution import chat_work_fields
    upstream_payload.update(await chat_work_fields(token_claims,conversation_id,upstream_payload['face_id']))
    from routers.execution import chat_pc_fields
    upstream_payload.update(await chat_pc_fields(token_claims,conversation_id,upstream_payload['face_id']))
    # Set only by the internal Agent Services handler; never forwarded from WS frames.
    if conv_session.get("agent_service_single_turn") is True:
        upstream_payload["agent_service_single_turn"] = True
    # Reasoning effort for a CLI seat that takes --effort (alternate Claude med/high/xhigh).
    # Set only by the internal Agent Services seat table, never from a client frame.
    if conv_session.get("effort"):
        upstream_payload["effort"] = conv_session["effort"]

    # U5 Ask-Bob helper bubble — forward the additive page_context snapshot when the bubble
    # sent one (no key added for an ordinary page_context-free turn ⇒ byte-identical upstream).
    upstream_payload.update(_page_context_field(payload))

    # MS9-W1 — forward the additive council-theater opt-in (no key added for an ordinary chat
    # turn ⇒ byte-identical upstream). Only the Council screen's start-turn sets it.
    upstream_payload.update(_emit_events_field(payload))

    # MS2-R2 — forward the additive research opt-in (no key added for an ordinary chat
    # turn ⇒ byte-identical upstream). The turn enters core's research orchestrator.
    upstream_payload.update(_research_field(payload))

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
                    try:
                        error = json.loads(body)
                    except (ValueError, TypeError):
                        error = {}
                    if not isinstance(error, dict):
                        error = {}
                    await _send_ws_error(ws, error.get('message') or body or "Upstream chat failed",
                                         error.get('code') or "upstream_error")
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
                            failure = {
                                'error': str(event.get('message') or 'upstream error'),
                                'error_type': str(event.get('error_type') or event.get('code') or 'upstream_error'),
                                'turn_id': event.get('turn_id') or uuid4().hex,
                            }
                            await _send_ws_error(
                                ws,
                                str(event.get("message") or "upstream error"),
                                str(event.get("code") or "upstream_error"),
                            )
                        elif event_type == "question":
                            # S2 (2026-10-27): the turn is parked in core until the
                            # operator answers. Forward the question verbatim; the
                            # answer comes back on POST /chat/questions/{id}/answer.
                            # Save preceding prose before the question is exposed:
                            # its answer is saved by core while this turn is parked.
                            if not turn_metadata and isinstance(event.get('request_id'), str) and event['request_id']:
                                turn_metadata['request_id'] = event['request_id']
                            preface = "".join(assistant_parts)
                            if preface.strip():
                                pre_question_saved = await _save_message(
                                    pool, conversation_id, "assistant", preface,
                                    {"kind": "pre_question", "question_id": event.get("question_id"),
                                     **({"origin": event["origin"]} if event.get("origin") else {}), **turn_metadata})
                                if pre_question_saved:
                                    pre_question_message_ids.append(str(pre_question_saved["id"]))
                                assistant_parts.clear()
                            await ws.send_json(event)
                        elif event_type in {'model_observation', 'model_usage'}:
                            if event_type == 'model_observation':
                                turn_metadata.update(backend=event.get('backend'),
                                    requested_model=event.get('model'),
                                    provider_reported_model=event.get('provider_reported_model'),
                                    model=event.get('provider_reported_model') or event.get('model'))
                            else:
                                turn_metadata['model_usage'] = event
                            await ws.send_json(event)
                        elif event_type in {"tool_activity", "builder_wait"}:
                            # S1 (2026-10-27): the tool-loop trail, forwarded verbatim.
                            # Additive by construction: the frame only appears on turns
                            # that used a tool, and a client that ignores unknown types
                            # sees exactly what it saw before.
                            await ws.send_json(event)
                        elif event_type in _COUNCIL_FRAME_TYPES:
                            # MS9-W1: relay the U7/U8 council theater frames VERBATIM. Core
                            # only emits them when the turn opted into emit_events, so an
                            # ordinary chat turn never reaches this branch (byte-identical).
                            # Additive: the chunk/approval/error/message_complete paths above
                            # are untouched, and a client ignoring unknown types is unaffected.
                            await ws.send_json(event)
                        elif event_type == "message_complete":
                            completion = event
                            turn_metadata.update({key: event[key] for key in (
                                'model', 'backend', 'requested_model', 'provider_reported_model', 'model_usage')
                                if key in event})
    except asyncio.CancelledError:
        if getattr(asyncio.current_task(), "_superseded", False):
            superseded = True
            await ws.send_json({"type": "generation_stopped", "code": "superseded"})
        pass

    assistant_message = "".join(assistant_parts)
    if failure and not assistant_message.strip():
        assistant_message = ('Execution failed (' + failure['error_type'] + ') — turn ' +
                             failure['turn_id'] + '; details in the server log.')
    saved = pre_question_saved
    if not superseded:
        # Pre-question prose is already durable, ahead of any operator answer.
        # Save only the remaining tail; a stop at the question needs no empty row.
        if assistant_message.strip() or pre_question_saved is None:
            saved = await _save_message(
                pool,
                conversation_id,
                "assistant",
                assistant_message,
                {
                    "tokens_in": (completion or {}).get("tokens_in", 0),
                    "tokens_out": (completion or {}).get("tokens_out", 0),
                    "step_extensions": (completion or {}).get("step_extensions", 0),
                    **turn_metadata,
                    **({"stopped_reason": completion["stopped_reason"]}
                       if (completion or {}).get("stopped_reason") else {}),
                    **(failure or {}),
                },
            )
        elapsed_ms = int((perf_counter() - started) * 1000)
        await ws.send_json(
            {
                "type": "message_complete",
                "step_extensions": (completion or {}).get("step_extensions", 0),
                "message_id": str((saved or {}).get("id") or (completion or {}).get("message_id") or ""),
                **turn_metadata,
                **({"pre_question_message_ids": pre_question_message_ids} if pre_question_message_ids else {}),
                **({"stopped_reason": completion["stopped_reason"]}
                   if (completion or {}).get("stopped_reason") else {}),
                "tokens_in": int((completion or {}).get("tokens_in", 0)),
                "tokens_out": int((completion or {}).get("tokens_out", 0)),
                "elapsed_ms": int((completion or {}).get("elapsed_ms", elapsed_ms)),
            }
        )


@router.post("/chat/questions/{question_id}/answer")
async def chat_question_answer(request: web.Request) -> web.Response:
    """S2: carry the operator's answer to the parked mid-turn question in core.

    The question id only exists for the turn that asked it; core answers 404 once
    it is resolved or gone, and this proxy passes that status through unchanged.
    """
    question_id = str(request.match_info.get("question_id") or "").strip()
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid_json"}, status=400)
    answer = str((body or {}).get("answer") or "").strip()
    if not question_id or not answer:
        return web.json_response({"error": "question_id and answer are required"}, status=400)

    url = f"{config.CORE_URL.rstrip('/')}/api/chat/questions/{question_id}/answer"
    import time
    from core.execution.chat_records import answer_signature
    now=int(time.time())
    if not request.get('user',{}).get('sub') or len(answer)>12000:
        return web.json_response({'error':'invalid_answer'},status=400)
    envelope={'question_id':question_id,'user_id':str(request['user']['sub']),
              'answer':answer,'iat':now,'exp':now+30}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json={'envelope':envelope,'vouch':answer_signature(envelope,config.BOBCLAW_SECRET)}, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                raw = await response.text()
                status = response.status
    except Exception:
        return web.json_response({"error": "core_unreachable"}, status=502)
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        return web.json_response({"error": "core_invalid_response"}, status=502)
    return web.json_response(payload, status=status)


@router.get("/ws/chat")
async def chat_socket(request: web.Request) -> web.StreamResponse:
    ws = CredentialWebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    payload, initial_message = await authenticate_ws(request, ws, allow_agent=True)
    if payload is None:
        return ws

    user_id = payload.get("sub", "admin")
    session_state = get_user_session(request.app, user_id)
    pool = request.app[POSTGRES_POOL_KEY]
    socket_streams = set()

    def _make_cleanup_stream_task(conv_session: dict, conversation_id: str):
        """Build a done-callback bound to one conversation's stream state."""

        def _cleanup_stream_task(task: asyncio.Task) -> None:
            """Called when a stream task finishes (naturally or by cancellation)."""
            socket_streams.discard(task)
            # Identity check: only the conversation's CURRENT active stream may
            # clear the state — a superseded task's late callback must not erase
            # its replacement (nor release the user's concurrency slot).
            if conv_session.get("active_stream") is task:
                conv_session.pop("active_stream", None)
                active_streams = session_state.get("active_streams")
                if active_streams is not None:
                    active_streams.discard(conversation_id)
            # task.exception() raises CancelledError on a cancelled task.
            if not task.cancelled():
                exc = task.exception()
                if exc is not None and not isinstance(exc, asyncio.CancelledError):
                    logger.exception("Stream task failed unexpectedly")

        return _cleanup_stream_task

    async def _handle_message(data: dict) -> bool:
        message_type = data.get("type")
        if message_type in {'message','switch_face','switch_profile','switch_locale','switch_model','stop_generation'}:
            from .conversations import workflow_owner
            run=await workflow_owner(pool,user_id,str(data.get('conversation_id') or ''))
            if run:
                await ws.send_json({'type':'error','code':'conversation_owned_by_run','run_id':run,
                    'message':'This conversation belongs to a workflow. Send steering or stop the run from its run view.'})
                return True

        if message_type == "switch_face":
            face_id = str(data.get("face_id") or "").strip()
            conversation_id = str(data.get("conversation_id") or "").strip()
            if not conversation_id:
                await _send_ws_error(ws, "conversation_id is required", "invalid_conversation")
                return True
            if not await _verify_conversation_access(pool, conversation_id, user_id):
                await _send_ws_error(ws, "Conversation not found or access denied", "not_found")
                return True
            if _face_denied_for_agent(payload, face_id):
                await _send_ws_error(ws, "Face is not in this token's allow-list", "face_forbidden")
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
            # Pin a saved profile (HOW layer) to this conversation — empty
            # clears it. The next turn's upstream payload reads
            # conv_session["profile"], so a council-shaped profile runs.
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
            if pool is not None:
                await pool.execute(
                    "UPDATE conversations SET profile = $2, updated_at = NOW() WHERE id = $1 AND user_id = $3",
                    conversation_id, profile or None, user_id,
                )
            await ws.send_json({"type": "profile_switched", "profile": profile or None})
            return True

        if message_type == "switch_locale":
            # Pin a locale to this conversation — empty clears it (defaults to
            # "en" upstream). The next turn's upstream payload reads
            # conv_session["locale"].
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
            if pool is not None:
                await pool.execute(
                    "UPDATE conversations SET locale = $2, updated_at = NOW() WHERE id = $1 AND user_id = $3",
                    conversation_id, locale or None, user_id,
                )
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
            from .models import validate_chat_pin
            try:
                await validate_chat_pin(backend, model)
            except ValueError as exc:
                await _send_ws_error(ws, str(exc), 'invalid_pin')
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
            conv_session = get_conversation_session(request.app, user_id, conversation_id)
            effective_face = data.get("face_id") or conv_session.get("face_id")
            if payload.get("token_type") == "agent" and not effective_face and pool is not None:
                row = await pool.fetchrow(
                    "SELECT face_id FROM conversations WHERE id = $1 AND user_id = $2 AND is_archived = FALSE",
                    conversation_id, user_id,
                )
                effective_face = row["face_id"] if row else None
            # Check before reserving a slot, saving the turn, or changing pins.
            if _face_denied_for_agent(payload, effective_face):
                await _send_ws_error(ws, "Face is not in this token's allow-list", "face_forbidden")
                return True
            active_streams = session_state.setdefault("active_streams", set())
            if (
                conversation_id not in active_streams
                and len(active_streams) >= config.MAX_CONCURRENT_STREAMS_PER_USER
            ):
                await _send_ws_error(ws, "Too many concurrent conversations", "concurrency_limit",
                                     reason="too_many_active_conversations")
                return True
            # Reserve the slot IMMEDIATELY (idempotent add): the history fetch and
            # message save below await, and a second connection passing the same
            # check before our add() would exceed the cap (audit 1A finding).
            reserved = conversation_id not in active_streams
            active_streams.add(conversation_id)
            vetted_face_id = None
            try:
                history = await _get_conversation_history(
                    pool, conversation_id,
                    limit=config.HISTORY_MESSAGE_COUNT,
                    max_chars=config.HISTORY_MAX_CHARS,
                )
                if payload.get("token_type") == "agent" and payload.get("faces"):
                    # Resolve again after history's await, BEFORE saving, mutating
                    # pins, or cancelling a prior stream. Freeze the vetted face:
                    # later DB awaits must not substitute a different routing choice.
                    row = await pool.fetchrow(
                        "SELECT face_id FROM conversations WHERE id = $1 AND user_id = $2 AND is_archived = FALSE",
                        conversation_id, user_id,
                    ) if pool is not None else None
                    vetted_face_id = (data.get("face_id") or conv_session.get("face_id")
                                      or (row["face_id"] if row else None) or "assistant")
                    if _face_denied_for_agent(payload, vetted_face_id):
                        if reserved:
                            active_streams.discard(conversation_id)
                        await _send_ws_error(ws, "Face is not in this token's allow-list", "face_forbidden")
                        return True
                request_id = data.get("request_id")
                await _save_message(pool, conversation_id, "user", content,
                                    {"request_id": request_id} if isinstance(request_id, str) and request_id else None)
            except Exception:
                if reserved:
                    active_streams.discard(conversation_id)
                raise
            if data.get("face_id"):
                conv_session["face_id"] = data["face_id"]
            if data.get("model"):
                conv_session["model"] = data["model"]
            # Cancel any existing stream in this conversation before starting a new one
            existing = conv_session.get("active_stream")
            if existing is not None and not existing.done():
                setattr(existing, "_superseded", True)
                existing.cancel()
            # Launch streaming as a background task so stop_generation can cancel it
            task = asyncio.create_task(
                _stream_chat_to_client(ws, conversation_id, data, conv_session, pool, user_id, history, payload, vetted_face_id)
            )
            task.add_done_callback(_make_cleanup_stream_task(conv_session, conversation_id))
            socket_streams.add(task)
            conv_session["active_stream"] = task
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
            except ConnectionResetError:
                raise  # socket notice failed; the outer receive loop ends quietly
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                # Core unreachable / timeout: surface an error frame instead of
                # letting the exception bubble out of _handle_message and silently
                # kill the WebSocket (the message path guards this; this one didn't).
                logger.warning("approval forwarding to core failed: %s", exc)
                await _send_ws_error(ws, "Approval service unavailable", "approval_error")
            return True

        if message_type == "stop_generation":
            conversation_id = str(data.get("conversation_id") or "").strip()
            if not conversation_id:
                await _send_ws_error(ws, "conversation_id is required", "invalid_conversation")
                return True
            if not await _verify_conversation_access(pool, conversation_id, user_id):
                await _send_ws_error(ws, "Conversation not found or access denied", "not_found")
                return True
            from core.builder.lifecycle import end_sessions
            from core.execution.api import lane_id
            await end_sessions(pool,lane_id(),user_id,[conversation_id])
            conv_session = get_conversation_session(request.app, user_id, conversation_id)
            active = conv_session.get("active_stream")
            if active is not None and not active.done():
                active.cancel()
                await ws.send_json({"type": "generation_stopped", "code": "stopped"})
            else:
                await ws.send_json({"type": "error", "message": "No active generation to stop", "code": "no_active_generation"})
            return True

        await _send_ws_error(ws, "Unsupported message type", "unsupported_message")
        return True

    try:
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
    except (ConnectionResetError, aiohttp.ClientConnectionError):
        # A notice racing the peer's disconnect is the end of this socket.
        pass
    finally:
        # A socket is one turn's transport, not the task's authorization lease.
        # Cancel its producer to avoid unexplained work after disconnection.
        # Durable pending proposals remain inspectable/approvable after reconnect.
        # Keep task identities: an older socket must not cancel a replacement
        # producer started by another socket in the same conversation.
        for active in tuple(socket_streams):
            if not active.done():
                active.cancel()

    return ws


_PIPE_NONCES_KEY = web.AppKey("pipe_nonces", dict)


@router.post("/chat/pipes")
async def pipes(request):
    """Internal core hop: the envelope itself authenticates this request."""
    import hashlib
    import time
    from core.permissions import (canonical_pipe_json, pipe_claims_valid,
                                  compute_pipe_vouch, verify_pipe_vouch)
    from routers import agent_services as svc

    try:
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise svc.ServiceError("bad_envelope", 401)
        if not isinstance(body, dict) or set(body) != {"claims", "vouch", "args"}:
            raise svc.ServiceError("bad_envelope", 401)
        claims, vouch, args = body["claims"], body["vouch"], body["args"]
        if not pipe_claims_valid(claims, check_time=False):
            raise svc.ServiceError("bad_envelope", 401)
        # Authenticate before disclosing expiry; never record unauthenticated nonces.
        import hmac
        expected = compute_pipe_vouch(claims, config.BOBCLAW_SECRET)
        if (not expected or not isinstance(vouch, str) or not vouch.isascii()
                or not hmac.compare_digest(expected, vouch)):
            raise svc.ServiceError("bad_envelope", 401)
        if claims["exp"] <= time.time():
            raise svc.ServiceError("envelope_expired", 401)
        if not verify_pipe_vouch(claims, vouch, config.BOBCLAW_SECRET):
            raise svc.ServiceError("bad_envelope", 401)
        if claims["op"] != "runs.get":
            raise svc.ServiceError("unknown_operation", 422)
        if not isinstance(args, dict) or set(args) != {"run_id"} or not isinstance(args["run_id"], str):
            raise svc.ServiceError("args_mismatch", 422)
        if hashlib.sha256(canonical_pipe_json(args).encode()).hexdigest() != claims["args_sha256"]:
            raise svc.ServiceError("args_mismatch", 422)
        # Slice 1B: in-memory, process-local TTL set; restart loses replay history.
        # No await between checking and consuming: concurrent requests cannot reuse it.
        nonces = request.app[_PIPE_NONCES_KEY]
        now = time.time()
        for nonce, expiry in list(nonces.items()):
            if expiry <= now:
                del nonces[nonce]
        if claims["nonce"] in nonces:
            raise svc.ServiceError("envelope_replayed", 409)
        nonces[claims["nonce"]] = claims["exp"]

        # Ownership for this hop is (same subject) AND (the same conversation that
        # dispatched the run). The agent-boundary owner string cannot be rebuilt
        # from an envelope, so it is not used here; a run started outside a
        # conversation is not readable from a conversation.
        row = svc._read_row(request.app, args["run_id"])
        if (str(row.get("owner", "")).split(":", 1)[0] != claims["sub"]
                or not row.get("conversation_id")
                or row.get("conversation_id") != claims["conversation_id"]):
            raise svc.ServiceError("run_foreign", 404)
        return web.json_response(dict(
            ok=True, operation_id=claims["nonce"], status=row["status"],
            receipt=dict(kind="run", id=row["id"], independently_verified=False),
            warnings=[]))
    except svc.ServiceError as exc:
        return svc.error(exc)
