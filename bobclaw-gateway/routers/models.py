import aiohttp
from aiohttp import web

from app_state import get_user_session
from config import config

router = web.RouteTableDef()


async def _proxy_json(method: str, path: str):
    url = f"{config.CORE_URL.rstrip('/')}" + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url) as response:
                if response.status >= 400:
                    body = await response.text()
                    raise web.HTTPBadGateway(
                        text=body or '{"error": "Core request failed"}',
                        content_type="application/json",
                    )
                return await response.json()
    except aiohttp.ClientConnectionError:
        # The core restarts after every model change; say so instead of a 500.
        raise web.HTTPServiceUnavailable(
            text='{"error": {"code": "core_restarting", "detail": "Bob is restarting its model service. Try again in a moment."}}',
            content_type="application/json", headers={"Retry-After": "2"}) from None


async def validate_chat_pin(backend, model):
    if not backend:
        return
    from core.lane_config import normalize_backend
    from core.model_choices import validate_pin
    try:
        rows = await _proxy_json('GET', '/api/models/available')
    except web.HTTPException:
        raise ValueError('Cannot check the offered models while the model service is unavailable. Refresh models and try again.') from None
    validate_pin(rows, normalize_backend(backend), model)


@router.get("/models/local")
async def list_local_models(request: web.Request) -> web.Response:
    payload = await _proxy_json("GET", "/api/models/local")
    return web.json_response(payload)


@router.get("/models/available")
async def list_available_models(request: web.Request) -> web.Response:
    payload = await _proxy_json("GET", "/api/models/available")
    return web.json_response(payload)


@router.get("/backends/catalog")
async def backends_catalog(request: web.Request) -> web.Response:
    """Unified router-view backend catalog (F3, 2026-10-01) — every backend
    core knows, classified cloud/cli/local with honest availability."""
    payload = await _proxy_json("GET", "/api/backends/catalog")
    return web.json_response(payload)


@router.post("/models/select")
async def select_model(request: web.Request) -> web.Response:
    body = await request.json()
    model = (body.get("model") or "").strip()
    backend = (body.get("backend") or "").strip()
    if not model or not backend:
        raise web.HTTPBadRequest(text='{"error": "model and backend are required"}', content_type="application/json")

    try:
        await validate_chat_pin(backend, model)
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc)) from None
    user_id = request["user"].get("sub", "admin")
    session_state = get_user_session(request.app, user_id)
    session_state["model"] = model
    session_state["backend"] = backend
    return web.json_response({"model": model, "backend": backend})
