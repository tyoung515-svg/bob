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


@router.get("/models/local")
async def list_local_models(request: web.Request) -> web.Response:
    payload = await _proxy_json("GET", "/api/models/local")
    return web.json_response(payload)


@router.get("/models/available")
async def list_available_models(request: web.Request) -> web.Response:
    payload = await _proxy_json("GET", "/api/models/available")
    return web.json_response(payload)


@router.post("/models/select")
async def select_model(request: web.Request) -> web.Response:
    body = await request.json()
    model = (body.get("model") or "").strip()
    backend = (body.get("backend") or "").strip()
    if not model or not backend:
        raise web.HTTPBadRequest(text='{"error": "model and backend are required"}', content_type="application/json")

    user_id = request["user"].get("sub", "admin")
    session_state = get_user_session(request.app, user_id)
    session_state["model"] = model
    session_state["backend"] = backend
    return web.json_response({"model": model, "backend": backend})
