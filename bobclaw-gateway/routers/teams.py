"""Gateway proxy for the core JOAT team store (list / create / delete).

Surfaces the built-in + custom team configs and lets the team-builder create or
delete custom teams. JWT-gated by the gateway ``auth_middleware`` like every other
route; forwards to core and passes the upstream response + status straight through
(e.g. a 400 invalid-team is NOT coerced to 502). Only an actual connection failure
to core surfaces as 502. Mirrors ``routers/routing_view.py``.
"""
import json

import aiohttp
from aiohttp import web

from config import config

router = web.RouteTableDef()


async def _proxy(method: str, path: str, *, json_body=None) -> web.Response:
    url = f"{config.CORE_URL.rstrip('/')}{path}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(method, url, json=json_body) as resp:
                body = await resp.read()
                return web.Response(
                    body=body, status=resp.status, content_type=resp.content_type
                )
    except aiohttp.ClientError as exc:
        raise web.HTTPBadGateway(
            text=json.dumps({"error": f"teams core request failed: {exc}"}),
            content_type="application/json",
        )


@router.get("/teams")
async def list_teams(request: web.Request) -> web.Response:
    if "contract_version" in request.query:
        from routers.execution import proxy, query
        try:
            return await proxy(request, "teams.list", query(request, {"cursor", "limit"}))
        except ValueError:
            return web.json_response({"error": {"code": "invalid_query"}}, status=422)
    return await _proxy("GET", "/api/teams")


@router.get("/backends")
async def list_backends(request: web.Request) -> web.Response:
    return await _proxy("GET", "/api/backends")


@router.post("/teams")
async def create_team(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid JSON body", "code": "invalid_json"}, status=400
        )
    if isinstance(body, dict) and "contract_version" in body:
        if type(body["contract_version"]) is not int or body["contract_version"] != 2:
            return web.json_response({"error": {"code": "unsupported_run_contract"}}, status=422)
        from routers.execution import proxy
        return await proxy(request, "teams.save", body)
    if request.get("user", {}).get("token_type") in {"agent", "portal"}:
        return web.json_response({"error": {"code": "client_upgrade_required"}}, status=403)
    return await _proxy("POST", "/api/teams", json_body=body)


@router.post("/teams/propose")
async def propose_team(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy("POST", "/api/teams/propose", json_body=body)


@router.post("/teams/refine")
async def refine_team(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = {}
    return await _proxy("POST", "/api/teams/refine", json_body=body)


# ── profiles (superset of teams) ────────────────────────────────────────────

@router.get("/profiles")
async def list_profiles(request: web.Request) -> web.Response:
    return await _proxy("GET", "/api/profiles")


@router.get("/profiles/{name}")
async def get_profile(request: web.Request) -> web.Response:
    return await _proxy("GET", f"/api/profiles/{request.match_info['name']}")


@router.post("/profiles")
async def create_profile(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": "invalid JSON body", "code": "invalid_json"}, status=400
        )
    return await _proxy("POST", "/api/profiles", json_body=body)


@router.delete("/profiles/{name}")
async def delete_profile(request: web.Request) -> web.Response:
    return await _proxy("DELETE", f"/api/profiles/{request.match_info['name']}")


@router.delete("/teams/{name}")
async def delete_team(request: web.Request) -> web.Response:
    return await _proxy("DELETE", f"/api/teams/{request.match_info['name']}")
