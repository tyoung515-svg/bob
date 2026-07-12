"""BoBClaw Gateway — memory-graph proxy (read-only, JWT-guarded).

``GET /memory/graph`` proxies to core ``GET /api/memory/graph``, forwarding the
query params (``nodes`` / ``k`` / ``floor`` / ``types``) and passing the upstream
response (body + status + content-type) straight through — a 400 invalid-request
is preserved, not coerced to 502. Only an actual core-connection failure surfaces
as 502 (same degrade posture as the sibling ``routing_view`` / ``memory`` proxies).

JWT-gated by the gateway ``auth_middleware`` like every other route
(``/memory/graph`` is not on the public allowlist). Read-only: a GET proxy only,
no mutation, no session/app-state writes.
"""
import json

import aiohttp
from aiohttp import web

from config import config

router = web.RouteTableDef()


@router.get("/memory/graph")
async def memory_graph(request: web.Request) -> web.Response:
    url = f"{config.CORE_URL.rstrip('/')}/api/memory/graph"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=dict(request.query)) as resp:
                body = await resp.read()
                return web.Response(
                    body=body,
                    status=resp.status,
                    content_type=resp.content_type,
                )
    except aiohttp.ClientError as exc:
        raise web.HTTPBadGateway(
            text=json.dumps({"error": f"memory-graph core request failed: {exc}"}),
            content_type="application/json",
        )
