"""Real HTTP gateway v2 forwarding, credential authority and legacy refusal."""
import time
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from auth import auth_middleware, create_access_token, create_agent_token, create_portal_token
from config import config
from core.execution.models import CAPABILITIES
from core.permissions import verify_execution_vouch
from routers import agent_services, teams


@pytest.fixture
async def proxy_client(monkeypatch):
    hits = []
    monkeypatch.setattr(config, "BOBCLAW_SECRET", "fixture-execution-secret")
    monkeypatch.setattr(config, "JWT_SECRET", "fixture-jwt-secret-at-least-32-characters")
    monkeypatch.setattr(agent_services, "local_status", AsyncMock(return_value={"available": False}))

    async def core(request):
        value = await request.json()
        assert verify_execution_vouch(value["envelope"], value["vouch"], config.BOBCLAW_SECRET)
        assert value["envelope"]["operation"] == request.match_info["op"]
        hits.append(value["envelope"])
        if request.match_info["op"] == "capabilities":
            return web.json_response({**CAPABILITIES, "storage_available": False, "execution_enabled": False,
                                      "blocked_reasons": ["execution_storage_or_executor_unavailable"]})
        return web.json_response({"received": value["envelope"]["args"]}, status=202 if request.match_info["op"] == "runs.start" else 200)

    app = web.Application()
    app.router.add_post("/api/execution/{op}", core)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(config, "CORE_URL", str(server.make_url("/")).rstrip("/"))
    gateway = web.Application(middlewares=[auth_middleware])
    gateway.add_routes(agent_services.router)
    gateway.add_routes(teams.router)
    from owner_sessions import router as owner_router
    gateway.add_routes(owner_router)
    client = TestClient(TestServer(gateway))
    await client.start_server()
    try:
        yield client, hits
    finally:
        await client.close()
        await server.close()


@pytest.mark.asyncio
async def test_v2_fields_and_identity_round_trip_without_gateway_task(proxy_client):
    client, hits = proxy_client
    headers = {"Authorization": "Bearer " + create_portal_token("lane-owner", {"may_touch": ["allowed/**"]})}
    body = {"contract_version": 2, "team": "saved-team", "team_revision": 3, "task": "supplied source",
            "request_key": "same-on-retry", "project_id": "project-ref",
            "budget": {"start_calls": 100, "wall_mode": "none", "wall_seconds": None}}
    response = await client.post("/agent-services/runs", headers=headers, json=body)
    assert response.status == 202 and (await response.json())["received"] == body
    assert hits[-1]["authority"]["owner"] == "lane-owner"
    assert hits[-1]["authority"]["actor"] == "lane-owner"
    assert hits[-1]["authority"]["scope"] == {"may_touch": ["allowed/**"]}
    assert hits[-1]["authority"]["token_type"] == "portal"
    assert hits[-1]["exp"] > time.time()
    assert "agent_services_tasks" not in client.app  # v2 needs no gateway task registry
    for path, params in (("/teams", {"contract_version": 2}),
                         ("/agent-services/runs", {"contract_version": 2, "limit": 5}),
                         ("/agent-services/runs/run-ref", {"contract_version": 2, "after_event_seq": 4})):
        assert (await client.get(path, headers=headers, params=params)).status == 200
    assert hits[-1]["args"] == {"run_id": "run-ref", "after_event_seq": 4}
    response = await client.post("/agent-services/runs/run-ref/cancel", headers=headers,
                                json={"contract_version": 2, "request_key": "cancel-same-key"})
    assert response.status == 200
    assert hits[-1]["args"] == {"run_id": "run-ref", "request_key": "cancel-same-key"}


@pytest.mark.asyncio
async def test_scoped_agent_can_save_v2_but_cannot_reach_legacy_writer(proxy_client):
    client, hits = proxy_client
    headers = {"Authorization": "Bearer " + create_agent_token("owner", faces=["researcher"], scope={"may_touch": ["one/**"]})}
    response = await client.post("/teams", headers=headers,
                                json={"contract_version": 2, "definition": {"id": "proposal"},
                                      "expected_revision": None, "request_key": "save-key"})
    assert response.status == 200
    assert hits[-1]["operation"] == "teams.save" and hits[-1]["authority"]["token_type"] == "agent"
    assert hits[-1]["authority"]["actor"] != "owner"
    before = len(hits)
    response = await client.post("/teams", headers=headers, json={"name": "legacy-write", "roles": {}})
    assert response.status == 403 and len(hits) == before
    response = await client.get("/agent-services/runs", headers=headers, params={"contract_version": 1})
    assert response.status == 403 and len(hits) == before


@pytest.mark.asyncio
async def test_owner_budget_and_resume_sign_exact_fields_and_deny_agents(proxy_client, monkeypatch):
    client, hits = proxy_client
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "owner")
    monkeypatch.setenv("BOB_EXECUTION_BOUNDARY", "worker")
    from db import init_db
    from tests.test_owner_sessions import stepup
    await init_db()
    portal = {"Authorization": "Bearer " + create_portal_token("owner", {}), "X-Bob-Broker-Link": "fixture-broker-link"}
    human, _ = await stepup(client, portal)
    for path, body, op in (
        ("/agent-services/runs/run-id/resume", {"contract_version":2,"expected_revision":41,"request_key":"resume-key",
                                             "budget":{"start_calls":100,"wall_mode":"none","wall_seconds":None}}, "runs.resume"),
        ("/agent-services/budgets", {"contract_version":2,"project_id":"p","call_limit":500,"expected_revision":None,"request_key":"pool-key"}, "budgets.set"),
    ):
        before = len(hits)
        for token in (create_access_token("owner"), create_agent_token("owner",faces=["researcher"]), create_portal_token("owner",{})):
            response = await client.post(path, headers={"Authorization":"Bearer "+token}, json=body)
            assert response.status in {403,404}
        assert len(hits) == before
        response = await client.post(path,headers=human,json=body)
        assert response.status == 200 and hits[-1]["operation"] == op
        expected = {k:v for k,v in body.items() if k != "contract_version"}
        if op == "runs.resume":
            expected["run_id"] = "run-id"
        assert hits[-1]["args"] == expected and hits[-1]["authority"]["token_type"] == "human"


@pytest.mark.asyncio
async def test_legacy_unsupported_fields_fail_before_spend(proxy_client):
    client, hits = proxy_client
    headers = {"Authorization": "Bearer " + create_agent_token("owner", faces=["researcher"])}
    for body in ({"team": "balance-budget", "task": "source", "budget": {"start_calls": 100}},
                 {"team": "balance-budget", "task": "source", "max_model_calls": 10},
                 {"team": "balance-budget", "task": "source", "max_seconds": 3600}):
        response = await client.post("/agent-services/runs", headers=headers, json=body)
        assert response.status == 422
    assert hits == []


@pytest.mark.asyncio
async def test_negotiated_catalog_advertises_live_storage_block(proxy_client):
    client, hits = proxy_client
    headers = {"Authorization": "Bearer " + create_portal_token("owner", {})}
    response = await client.get("/agent-services/catalog", headers=headers, params={"contract_version": 2})
    assert response.status == 200
    capabilities = (await response.json())["capabilities"]["execution_v2"]
    assert capabilities["contract_versions"] == [2]
    assert capabilities["execution_enabled"] is False
    assert capabilities["blocked_reasons"] == ["execution_storage_or_executor_unavailable"]
    assert hits[-1]["operation"] == "capabilities"


@pytest.mark.asyncio
async def test_operation_advertisement_is_versioned_and_preserves_legacy(monkeypatch):
    # Owner operations are advertised as unavailable when no owner is configured.
    monkeypatch.delenv("BOB_DEFAULT_OWNER", raising=False)
    from aiohttp.test_utils import make_mocked_request
    from routers.capabilities import get_capabilities
    import json
    legacy = await get_capabilities(make_mocked_request("GET", "/capabilities?operations=1"))
    assert set(json.loads(legacy.text)["operations"]) == {"memory.list", "files.roots", "files.list", "files.read"}
    versioned = await get_capabilities(make_mocked_request("GET", "/capabilities?operations=1&contract_version=2"))
    operations = json.loads(versioned.text)["operations"]
    assert operations["runs.start"]["request_key_required"] and operations["runs.list"]["version"] == 2
