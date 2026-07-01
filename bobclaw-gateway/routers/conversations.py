import logging
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from aiohttp import web

from app_state import POSTGRES_POOL_KEY

logger = logging.getLogger(__name__)

router = web.RouteTableDef()


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
    return _jsonable(dict(record))


def _get_pool(request: web.Request):
    pool = request.app[POSTGRES_POOL_KEY]
    if pool is None:
        raise web.HTTPServiceUnavailable(text='{"error": "Postgres unavailable"}', content_type="application/json")
    return pool


def _get_user_id(request: web.Request) -> str:
    return request.get("user", {}).get("sub", "admin")


@router.get("/conversations")
async def list_conversations(request: web.Request) -> web.Response:
    limit = max(1, min(int(request.query.get("limit", "20")), 100))
    offset = max(0, int(request.query.get("offset", "0")))
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    rows = await pool.fetch(
        """
        SELECT c.id, c.title, c.face_id, c.model_preference, c.backend_preference, c.project_id, c.updated_at,
               (
                   SELECT LEFT(m.content, 120)
                   FROM messages m
                   WHERE m.conversation_id = c.id
                   ORDER BY m.created_at DESC, m.id DESC
                   LIMIT 1
               ) AS last_message_preview
        FROM conversations c
        WHERE c.is_archived = FALSE
          AND c.user_id = $3
        ORDER BY c.updated_at DESC
        LIMIT $1 OFFSET $2
        """,
        limit,
        offset,
        user_id,
    )
    return web.json_response({
        "items": [_record_to_dict(row) for row in rows],
        "limit": limit,
        "offset": offset,
    })


@router.post("/conversations")
async def create_conversation(request: web.Request) -> web.Response:
    body = await request.json()
    title = (body.get("title") or "New Conversation").strip() or "New Conversation"
    face_id = body.get("face_id")
    model_preference = body.get("model_preference")
    project_id = body.get("project_id") or None
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    backend_preference = None
    if project_id is not None:
        project = await pool.fetchrow(
            "SELECT default_face_id, default_backend FROM projects WHERE id = $1 AND user_id = $2 AND is_archived = FALSE",
            project_id,
            user_id,
        )
        if project is None:
            raise web.HTTPBadRequest(text='{"error": "unknown project_id"}', content_type="application/json")
        # Inherit the project's defaults: only fall back to the project face when
        # the request didn't pin one; backend always follows the project default.
        if not face_id:
            face_id = project["default_face_id"]
        backend_preference = project["default_backend"]

    row = await pool.fetchrow(
        """
        INSERT INTO conversations (user_id, title, face_id, model_preference, backend_preference, project_id)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, user_id, title, face_id, model_preference, backend_preference, project_id, updated_at, is_archived
        """,
        user_id,
        title,
        face_id,
        model_preference,
        backend_preference,
        project_id,
    )
    return web.json_response(_record_to_dict(row), status=201)


@router.get("/conversations/{conv_id}")
async def get_conversation(request: web.Request) -> web.Response:
    conv_id = request.match_info["conv_id"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    row = await pool.fetchrow(
        """
        SELECT id, title, face_id, model_preference, backend_preference, project_id, updated_at, is_archived, user_id
        FROM conversations
        WHERE id = $1 AND user_id = $2
        """,
        conv_id,
        user_id,
    )
    if row is None or row["is_archived"]:
        raise web.HTTPNotFound()
    return web.json_response(_record_to_dict(row))


@router.get("/conversations/{conv_id}/messages")
async def list_messages(request: web.Request) -> web.Response:
    conv_id = request.match_info["conv_id"]
    limit = max(1, min(int(request.query.get("limit", "50")), 100))
    before = request.query.get("before")
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    # Verify conversation ownership before returning messages
    conv = await pool.fetchrow(
        "SELECT id FROM conversations WHERE id = $1 AND user_id = $2",
        conv_id,
        user_id,
    )
    if conv is None:
        raise web.HTTPNotFound()

    cursor_created_at = None
    if before:
        cursor = await pool.fetchrow(
            "SELECT id, created_at FROM messages WHERE conversation_id = $1 AND id = $2",
            conv_id,
            before,
        )
        if cursor is None:
            raise web.HTTPBadRequest(text='{"error": "Invalid cursor"}', content_type="application/json")
        cursor_created_at = cursor["created_at"]

    rows = await pool.fetch(
        """
        SELECT id, conversation_id, role, content, created_at, metadata
        FROM messages
        WHERE conversation_id = $1
          AND (
              $2::timestamptz IS NULL
              OR created_at < $2
              OR (created_at = $2 AND id < $3)
          )
        ORDER BY created_at DESC, id DESC
        LIMIT $4
        """,
        conv_id,
        cursor_created_at,
        before,
        limit + 1,
    )
    has_more = len(rows) > limit
    items = rows[:limit]
    return web.json_response({
        "items": [_record_to_dict(row) for row in items],
        "has_more": has_more,
    })


@router.delete("/conversations/{conv_id}")
async def archive_conversation(request: web.Request) -> web.Response:
    conv_id = request.match_info["conv_id"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    result = await pool.execute(
        "UPDATE conversations SET is_archived = TRUE, updated_at = NOW() WHERE id = $1 AND user_id = $2 AND is_archived = FALSE",
        conv_id,
        user_id,
    )
    if result.endswith("0"):
        raise web.HTTPNotFound()
    return web.json_response({"status": "archived", "conversation_id": conv_id})


@router.post("/conversations/{conv_id}/rename")
async def rename_conversation(request: web.Request) -> web.Response:
    conv_id = request.match_info["conv_id"]
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        raise web.HTTPBadRequest(text='{"error": "title is required"}', content_type="application/json")

    pool = _get_pool(request)
    user_id = _get_user_id(request)
    row = await pool.fetchrow(
        """
        UPDATE conversations
        SET title = $2, updated_at = NOW()
        WHERE id = $1 AND user_id = $3 AND is_archived = FALSE
        RETURNING id, user_id, title, face_id, model_preference, backend_preference, project_id, updated_at, is_archived
        """,
        conv_id,
        title,
        user_id,
    )
    if row is None:
        raise web.HTTPNotFound()
    return web.json_response(_record_to_dict(row))


@router.post("/conversations/{conv_id}/project")
async def assign_conversation_project(request: web.Request) -> web.Response:
    conv_id = request.match_info["conv_id"]
    body = await request.json()
    # null or "" means unassign (back to no project).
    project_id = body.get("project_id") or None
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    # Verify the conversation exists and is owned by the caller.
    conv = await pool.fetchrow(
        "SELECT id FROM conversations WHERE id = $1 AND user_id = $2 AND is_archived = FALSE",
        conv_id,
        user_id,
    )
    if conv is None:
        raise web.HTTPNotFound()

    # When assigning, the target project must exist and be owned by the caller.
    if project_id is not None:
        project = await pool.fetchrow(
            "SELECT id FROM projects WHERE id = $1 AND user_id = $2 AND is_archived = FALSE",
            project_id,
            user_id,
        )
        if project is None:
            raise web.HTTPBadRequest(text='{"error": "unknown project_id"}', content_type="application/json")

    row = await pool.fetchrow(
        """
        UPDATE conversations
        SET project_id = $2, updated_at = NOW()
        WHERE id = $1 AND user_id = $3 AND is_archived = FALSE
        RETURNING id, user_id, title, face_id, model_preference, backend_preference, project_id, updated_at, is_archived
        """,
        conv_id,
        project_id,
        user_id,
    )
    if row is None:
        raise web.HTTPNotFound()
    return web.json_response(_record_to_dict(row))
