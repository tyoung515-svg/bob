"""
BoBClaw Gateway — Memory facts proxy.

Forwards the memory browser's calls (JWT-protected by auth_middleware) to core:
    GET    /memory/facts        → core GET    /api/memory/facts
    DELETE /memory/facts/{id}   → core DELETE /api/memory/facts/{id}

Unlike faces/models, this proxy forwards the upstream HTTP status (so a 404
"unknown fact" or 503 "memory disabled" reaches the client as-is) and only
maps a genuine core-connection failure to 502.
"""
import json

import aiohttp
from aiohttp import web

from config import config

router = web.RouteTableDef()


async def _proxy(method: str, path: str, params: dict | None = None) -> tuple[int, str]:
    url = f"{config.CORE_URL.rstrip('/')}" + path
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, params=params) as response:
                return response.status, await response.text()
    except aiohttp.ClientError as exc:
        raise web.HTTPBadGateway(
            text=json.dumps({"error": f"core unreachable: {exc}"}),
            content_type="application/json",
        )


@router.get("/memory/facts")
async def list_memory_facts(request: web.Request) -> web.Response:
    params: dict = {}
    if "limit" in request.query:
        params["limit"] = request.query["limit"]
    if "offset" in request.query:
        params["offset"] = request.query["offset"]
    status, body = await _proxy("GET", "/api/memory/facts", params or None)
    return web.Response(status=status, text=body, content_type="application/json")


@router.delete("/memory/facts/{fact_id}")
async def forget_memory_fact(request: web.Request) -> web.Response:
    fact_id = request.match_info["fact_id"]
    status, body = await _proxy("DELETE", f"/api/memory/facts/{fact_id}")
    return web.Response(status=status, text=body, content_type="application/json")
