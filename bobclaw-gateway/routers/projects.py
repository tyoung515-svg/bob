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


@router.get("/projects")
async def list_projects(request: web.Request) -> web.Response:
    limit = max(1, min(int(request.query.get("limit", "20")), 100))
    offset = max(0, int(request.query.get("offset", "0")))
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    rows = await pool.fetch(
        """
        SELECT p.id, p.name, p.description, p.default_face_id, p.default_backend, p.updated_at,
               (
                   SELECT COUNT(*)
                   FROM conversations c
                   WHERE c.project_id = p.id AND c.is_archived = FALSE
               ) AS conversation_count
        FROM projects p
        WHERE p.user_id = $3 AND p.is_archived = FALSE
        ORDER BY p.updated_at DESC
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


@router.post("/projects")
async def create_project(request: web.Request) -> web.Response:
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise web.HTTPBadRequest(text='{"error": "name is required"}', content_type="application/json")
    description = body.get("description")
    instructions = body.get("instructions")
    default_face_id = body.get("default_face_id")
    default_backend = body.get("default_backend")
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    row = await pool.fetchrow(
        """
        INSERT INTO projects (user_id, name, description, instructions, default_face_id, default_backend)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id, user_id, name, description, instructions, default_face_id, default_backend, is_archived, created_at, updated_at
        """,
        user_id,
        name,
        description,
        instructions,
        default_face_id,
        default_backend,
    )
    return web.json_response(_record_to_dict(row), status=201)


@router.get("/projects/{project_id}")
async def get_project(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    row = await pool.fetchrow(
        """
        SELECT id, user_id, name, description, instructions, default_face_id, default_backend, is_archived, created_at, updated_at
        FROM projects
        WHERE id = $1 AND user_id = $2
        """,
        project_id,
        user_id,
    )
    if row is None or row["is_archived"]:
        raise web.HTTPNotFound()
    return web.json_response(_record_to_dict(row))


@router.post("/projects/{project_id}")
async def update_project(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    body = await request.json()
    pool = _get_pool(request)
    user_id = _get_user_id(request)

    updatable = ("name", "description", "instructions", "default_face_id", "default_backend")
    set_clauses = []
    args = [project_id]
    for key in updatable:
        if key in body:
            value = body[key]
            if key == "name":
                value = (value or "").strip()
                if not value:
                    raise web.HTTPBadRequest(text='{"error": "name cannot be blank"}', content_type="application/json")
            args.append(value)
            set_clauses.append(f"{key} = ${len(args)}")

    set_clauses.append("updated_at = NOW()")
    args.append(user_id)
    user_id_param = len(args)

    row = await pool.fetchrow(
        f"""
        UPDATE projects
        SET {", ".join(set_clauses)}
        WHERE id = $1 AND user_id = ${user_id_param} AND is_archived = FALSE
        RETURNING id, user_id, name, description, instructions, default_face_id, default_backend, is_archived, created_at, updated_at
        """,
        *args,
    )
    if row is None:
        raise web.HTTPNotFound()
    return web.json_response(_record_to_dict(row))


@router.delete("/projects/{project_id}")
async def archive_project(request: web.Request) -> web.Response:
    project_id = request.match_info["project_id"]
    pool = _get_pool(request)
    user_id = _get_user_id(request)
    async with pool.acquire() as con:
        async with con.transaction():
            result = await con.execute(
                "UPDATE projects SET is_archived=TRUE, updated_at=NOW() WHERE id=$1 AND user_id=$2 AND is_archived=FALSE",
                project_id,
                user_id,
            )
            if result.endswith("0"):
                raise web.HTTPNotFound()
            await con.execute(
                "UPDATE conversations SET project_id=NULL, updated_at=NOW() WHERE project_id=$1 AND user_id=$2",
                project_id,
                user_id,
            )
    return web.json_response({"status": "archived", "project_id": project_id})
