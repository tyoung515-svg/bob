"""Gateway proxy for the core JOAT v0 routing-view (read-only).

Surfaces the live faces → roles → resolved-backends map + active team for the
KMM/web routing view. JWT-gated by the gateway ``auth_middleware`` like every
other route. Forwards ``?team`` / ``?format`` to core and passes the upstream
response (JSON or text/plain table) straight through, preserving its status code
(e.g. a 400 unknown-team is NOT coerced to 502). Only an actual connection
failure to core surfaces as 502.
"""
import json

import aiohttp
from aiohttp import web

from config import config

router = web.RouteTableDef()


@router.get("/routing-view")
async def routing_view(request: web.Request) -> web.Response:
    url = f"{config.CORE_URL.rstrip('/')}/api/routing-view"
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
            text=json.dumps({"error": f"routing-view core request failed: {exc}"}),
            content_type="application/json",
        )
