"""
BoBClaw Gateway — /agents routes (Hermes-shaped interface, Wave 2 Task 1).

A named teammate ("bot") = face + optional profile + one canonical conversation,
persisted as an agent_bindings row. The binding row is the identity; the
conversation title ("Bot: <slug>") is display-only.

Archive policy (explicit): DELETE /agents/{slug} soft-archives BOTH the binding
and its conversation. The archived binding keeps its (user_id, slug) row, so
re-creating the same slug returns 409 — pick a new slug; there is no restore
endpoint in this wave. This keeps slug history unambiguous and the
conversation_id-per-binding invariant intact.
"""
import json
import logging
from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from aiohttp import web

try:
    from asyncpg import UniqueViolationError
except ImportError:  # test env without asyncpg — the catch becomes unreachable
    class UniqueViolationError(Exception):  # type: ignore[no-redef]
        pass

from app_state import POSTGRES_POOL_KEY
from core.faces.registry import get_default_registry

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


def _with_face_display(item: dict) -> dict:
    """Join the faces registry for display-only fields (avatar, face_name)."""
    registry = get_default_registry()
    face = registry.get_face(item["face_id"]) if item.get("face_id") in registry else None
    item["avatar"] = face.avatar if face else None
    item["face_name"] = (face.display_name or face.name) if face else None
    return item


@router.get("/agents")
async def list_agents(request: web.Request) -> web.Response:
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    rows = await pool.fetch(
        """
        SELECT id, user_id, slug, display_name, face_id, profile_name, conversation_id, ui_meta, is_archived, created_at, updated_at
        FROM agent_bindings
        WHERE user_id = $1 AND is_archived = FALSE
        ORDER BY created_at ASC
        """,
        user_id,
    )
    return web.json_response({
        "items": [_with_face_display(_record_to_dict(row)) for row in rows],
    })


@router.post("/agents")
async def create_agent(request: web.Request) -> web.Response:
    body = await request.json()
    slug = (body.get("slug") or "").strip()
    face_id = (body.get("face_id") or "").strip()
    display_name = (body.get("display_name") or "").strip() or slug
    profile_name = body.get("profile_name") or None
    if not slug or not face_id:
        raise web.HTTPBadRequest(text='{"error": "slug and face_id are required"}', content_type="application/json")
    if face_id not in get_default_registry():
        raise web.HTTPBadRequest(text='{"error": "unknown face_id"}', content_type="application/json")

    pool = _get_pool(request)
    user_id = _get_user_id(request)

    async with pool.acquire() as con:
        async with con.transaction():
            existing = await con.fetchrow(
                "SELECT id, is_archived FROM agent_bindings WHERE user_id = $1 AND slug = $2",
                user_id,
                slug,
            )
            if existing is not None:
                if existing["is_archived"]:
                    raise web.HTTPConflict(
                        text=json.dumps({"error": f"slug '{slug}' belongs to an archived agent; pick a new slug (archived bindings are not restored)"}),
                        content_type="application/json",
                    )
                raise web.HTTPConflict(
                    text=json.dumps({"error": f"slug '{slug}' already in use"}),
                    content_type="application/json",
                )
            try:
                conversation = await con.fetchrow(
                    """
                    INSERT INTO conversations (user_id, title, face_id, model_preference, backend_preference, project_id)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id, user_id, title, face_id, model_preference, backend_preference, project_id, updated_at, is_archived
                    """,
                    user_id,
                    f"Bot: {slug}",
                    face_id,
                    None,
                    None,
                    None,
                )
                binding = await con.fetchrow(
                    """
                    INSERT INTO agent_bindings (user_id, slug, display_name, face_id, profile_name, conversation_id)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    RETURNING id, user_id, slug, display_name, face_id, profile_name, conversation_id, ui_meta, is_archived, created_at, updated_at
                    """,
                    user_id,
                    slug,
                    display_name,
                    face_id,
                    profile_name,
                    conversation["id"],
                )
            except UniqueViolationError:
                # TOCTOU: a concurrent request created the same slug between the
                # pre-check and the insert — translate to the intended 409 (audit P2).
                raise web.HTTPConflict(
                    text=json.dumps({"error": f"slug '{slug}' already in use"}),
                    content_type="application/json",
                ) from None
    return web.json_response(_with_face_display(_record_to_dict(binding)), status=201)


@router.get("/agents/{slug}")
async def get_agent(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    row = await pool.fetchrow(
        """
        SELECT id, user_id, slug, display_name, face_id, profile_name, conversation_id, ui_meta, is_archived, created_at, updated_at
        FROM agent_bindings
        WHERE user_id = $1 AND slug = $2 AND is_archived = FALSE
        """,
        user_id,
        slug,
    )
    if row is None:
        raise web.HTTPNotFound()
    return web.json_response(_with_face_display(_record_to_dict(row)))


@router.delete("/agents/{slug}")
async def archive_agent(request: web.Request) -> web.Response:
    slug = request.match_info["slug"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    async with pool.acquire() as con:
        async with con.transaction():
            binding = await con.fetchrow(
                """
                UPDATE agent_bindings
                SET is_archived = TRUE, updated_at = NOW()
                WHERE user_id = $1 AND slug = $2 AND is_archived = FALSE
                RETURNING id, conversation_id
                """,
                user_id,
                slug,
            )
            if binding is None:
                raise web.HTTPNotFound()
            await con.execute(
                "UPDATE conversations SET is_archived = TRUE, updated_at = NOW() WHERE id = $1 AND user_id = $2 AND is_archived = FALSE",
                binding["conversation_id"],
                user_id,
            )
    return web.json_response({
        "status": "archived",
        "slug": slug,
        "conversation_id": str(binding["conversation_id"]),
    })
