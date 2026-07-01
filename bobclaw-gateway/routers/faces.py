import aiohttp
from aiohttp import web

from app_state import get_user_session
from config import config

router = web.RouteTableDef()


async def _proxy_json(method: str, path: str):
    url = f"{config.CORE_URL.rstrip('/')}" + path
    async with aiohttp.ClientSession() as session:
        async with session.request(method, url) as response:
            if response.status >= 400:
                body = await response.text()
                raise web.HTTPBadGateway(
                    text=body or '{"error": "Core request failed"}',
                    content_type="application/json",
                )
            return await response.json()


@router.get("/faces/active")
async def get_active_face(request: web.Request) -> web.Response:
    user_id = request["user"].get("sub", "admin")
    session_state = get_user_session(request.app, user_id)
    return web.json_response(
        {
            "face_id": session_state.get("face_id"),
            "face_name": session_state.get("face_name"),
        }
    )


@router.get("/faces")
async def list_faces(request: web.Request) -> web.Response:
    payload = await _proxy_json("GET", "/api/faces")
    return web.json_response(payload)


@router.get("/faces/{face_id}")
async def get_face(request: web.Request) -> web.Response:
    payload = await _proxy_json("GET", f"/api/faces/{request.match_info['face_id']}")
    return web.json_response(payload)
