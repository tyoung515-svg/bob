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


@router.get("/bob-guide")
async def bob_guide(request: web.Request) -> web.Response:
    """Read one lane-local topic with server-verified face scope; never read owner data."""
    claims = request['user']
    roster = await _proxy_json('GET', '/api/faces')
    # Keep guide disclosure within an explicit token allowlist even when legacy
    # household-open chat routing is enabled. Never trust client-supplied visibility.
    allowed = claims.get('faces')
    if allowed is None:
        allowed = []
    if not isinstance(allowed, list) or len(allowed) > 32 or any(not isinstance(f, str) for f in allowed):
        raise web.HTTPForbidden(text='invalid guide face scope')
    visible = [f['id'] for f in roster if not allowed or f['id'] in allowed]
    face = request.query.get('face_id') or None
    if face is not None and face not in visible:
        raise web.HTTPForbidden(text='face outside guide scope')
    body = dict(topic=request.query.get('topic', 'index'), face_id=face, visible_faces=visible)
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
        async with session.post(config.CORE_URL.rstrip('/') + '/api/bob-guide', json=body) as response:
            if response.status == 403:
                raise web.HTTPForbidden(text='face outside guide scope')
            if response.status == 400:
                raise web.HTTPBadRequest(text='invalid guide topic')
            if response.status != 200:
                raise web.HTTPBadGateway(text='guide unavailable')
            payload = await response.json()
    if body['topic'] == 'limits':
        payload['content'] += '\nGateway concurrent streams per user=' + str(config.MAX_CONCURRENT_STREAMS_PER_USER)
    return web.json_response(payload)
