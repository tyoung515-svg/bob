"""Gateway proxy for the core Flight supervisor registry (Lane 1a).

Surfaces ``/flights`` (list/create/get/update/delete) for the TUI / web / KMM. JWT-gated
by the gateway ``auth_middleware`` like every other route. Forwards method + query + JSON
body to core ``/api/flights`` and passes the upstream response straight through
(status + content-type preserved — a 400/404 is not coerced to 502). Only an actual
connection failure to core surfaces as 502.
"""
import json

import aiohttp
from aiohttp import web

from config import config

router = web.RouteTableDef()


async def _proxy(request: web.Request, core_path: str) -> web.Response:
    url = f"{config.CORE_URL.rstrip('/')}{core_path}"
    data = await request.read()
    headers = {}
    if request.content_type:
        headers["Content-Type"] = request.content_type
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                request.method, url, params=dict(request.query),
                data=data or None, headers=headers,
            ) as resp:
                body = await resp.read()
                return web.Response(body=body, status=resp.status,
                                    content_type=resp.content_type)
    except aiohttp.ClientError as exc:
        raise web.HTTPBadGateway(
            text=json.dumps({"error": f"flights core request failed: {exc}"}),
            content_type="application/json",
        )


@router.get("/flights")
async def list_flights(request: web.Request) -> web.Response:
    return await _proxy(request, "/api/flights")


@router.post("/flights")
async def create_flight(request: web.Request) -> web.Response:
    return await _proxy(request, "/api/flights")


@router.get("/flights/{flight_id}")
async def get_flight(request: web.Request) -> web.Response:
    return await _proxy(request, f"/api/flights/{request.match_info['flight_id']}")


@router.patch("/flights/{flight_id}")
async def update_flight(request: web.Request) -> web.Response:
    return await _proxy(request, f"/api/flights/{request.match_info['flight_id']}")


@router.delete("/flights/{flight_id}")
async def delete_flight(request: web.Request) -> web.Response:
    return await _proxy(request, f"/api/flights/{request.match_info['flight_id']}")
