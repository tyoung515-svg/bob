"""
BoBClaw Gateway — Ideas (ADHD parking lot)

Tenant-isolated CRUD for the dashboard idea-inbox tile.
States: raw → triaged → {active, parked} → archived
"""
import logging
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from aiohttp import web

from app_state import POSTGRES_POOL_KEY

logger = logging.getLogger(__name__)

router = web.RouteTableDef()

_VALID_STATES = {"raw", "triaged", "active", "parked", "archived"}
_NON_ARCHIVED_STATES = _VALID_STATES - {"archived"}


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


@router.get("/ideas")
async def list_ideas(request: web.Request) -> web.Response:
    limit = max(1, min(int(request.query.get("limit", "50")), 200))
    offset = max(0, int(request.query.get("offset", "0")))
    state_filter = request.query.get("state")
    if state_filter is not None and state_filter not in _VALID_STATES:
        raise web.HTTPBadRequest(text='{"error": "Invalid state"}', content_type="application/json")
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    if state_filter:
        rows = await pool.fetch(
            """
            SELECT id, user_id, body, tags, state, promoted_to, created_at, updated_at
            FROM ideas
            WHERE user_id = $1 AND state = $2
            ORDER BY updated_at DESC
            LIMIT $3 OFFSET $4
            """,
            user_id, state_filter, limit, offset,
        )
    else:
        rows = await pool.fetch(
            """
            SELECT id, user_id, body, tags, state, promoted_to, created_at, updated_at
            FROM ideas
            WHERE user_id = $1 AND state != 'archived'
            ORDER BY updated_at DESC
            LIMIT $2 OFFSET $3
            """,
            user_id, limit, offset,
        )
    return web.json_response({
        "items": [_record_to_dict(row) for row in rows],
        "limit": limit,
        "offset": offset,
    })


@router.post("/ideas")
async def create_idea(request: web.Request) -> web.Response:
    body_json = await request.json()
    body = (body_json.get("body") or "").strip()
    if not body:
        raise web.HTTPBadRequest(text='{"error": "body is required"}', content_type="application/json")
    tags = body_json.get("tags") or []
    if not isinstance(tags, list):
        raise web.HTTPBadRequest(text='{"error": "tags must be an array"}', content_type="application/json")
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    row = await pool.fetchrow(
        """
        INSERT INTO ideas (user_id, body, tags)
        VALUES ($1, $2, $3)
        RETURNING id, user_id, body, tags, state, promoted_to, created_at, updated_at
        """,
        user_id, body, tags,
    )
    return web.json_response(_record_to_dict(row), status=201)


@router.get("/ideas/by-state")
async def ideas_by_state(request: web.Request) -> web.Response:
    """Return non-archived ideas grouped by state with counts + 5 most-recent each."""
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    rows = await pool.fetch(
        """
        SELECT id, user_id, body, tags, state, promoted_to, created_at, updated_at
        FROM ideas
        WHERE user_id = $1 AND state != 'archived'
        ORDER BY updated_at DESC
        """,
        user_id,
    )
    grouped: dict[str, dict] = {state: {"count": 0, "recent": []} for state in _NON_ARCHIVED_STATES}
    for row in rows:
        state = row["state"]
        bucket = grouped.setdefault(state, {"count": 0, "recent": []})
        bucket["count"] += 1
        if len(bucket["recent"]) < 5:
            bucket["recent"].append(_record_to_dict(row))
    return web.json_response(grouped)


@router.get("/ideas/{idea_id}")
async def get_idea(request: web.Request) -> web.Response:
    idea_id = request.match_info["idea_id"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    row = await pool.fetchrow(
        """
        SELECT id, user_id, body, tags, state, promoted_to, created_at, updated_at
        FROM ideas
        WHERE id = $1 AND user_id = $2
        """,
        idea_id, user_id,
    )
    if row is None:
        raise web.HTTPNotFound()
    return web.json_response(_record_to_dict(row))


@router.patch("/ideas/{idea_id}")
async def update_idea(request: web.Request) -> web.Response:
    idea_id = request.match_info["idea_id"]
    body_json = await request.json()
    body = body_json.get("body")
    tags = body_json.get("tags")
    state = body_json.get("state")
    promoted_to = body_json.get("promoted_to")

    if state is not None and state not in _VALID_STATES:
        raise web.HTTPBadRequest(text='{"error": "Invalid state"}', content_type="application/json")
    if tags is not None and not isinstance(tags, list):
        raise web.HTTPBadRequest(text='{"error": "tags must be an array"}', content_type="application/json")
    if body is not None and not str(body).strip():
        raise web.HTTPBadRequest(text='{"error": "body cannot be empty"}', content_type="application/json")

    pool = _get_pool(request)
    user_id = _get_user_id(request)

    row = await pool.fetchrow(
        """
        UPDATE ideas SET
            body = COALESCE($2, body),
            tags = COALESCE($3::jsonb, tags),
            state = COALESCE($4, state),
            promoted_to = COALESCE($5::uuid, promoted_to),
            updated_at = NOW()
        WHERE id = $1 AND user_id = $6
        RETURNING id, user_id, body, tags, state, promoted_to, created_at, updated_at
        """,
        idea_id,
        body,
        tags,
        state,
        promoted_to,
        user_id,
    )
    if row is None:
        raise web.HTTPNotFound()
    return web.json_response(_record_to_dict(row))


@router.delete("/ideas/{idea_id}")
async def archive_idea(request: web.Request) -> web.Response:
    idea_id = request.match_info["idea_id"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    result = await pool.execute(
        "UPDATE ideas SET state = 'archived', updated_at = NOW() "
        "WHERE id = $1 AND user_id = $2 AND state != 'archived'",
        idea_id, user_id,
    )
    if result.endswith("0"):
        raise web.HTTPNotFound()
    return web.json_response({"status": "archived", "idea_id": idea_id})
