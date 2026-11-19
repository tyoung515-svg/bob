"""Real gateway -> core -> Postgres protocol with fake models only.

Set S4B_TEST_POSTGRES_DSN to a disposable LOCAL Postgres database. Each test uses
and drops its own random schema; no production tables or provider calls. These
tests deliberately skip without Postgres instead of substituting memory/SQLite.
"""
import asyncio
from datetime import datetime, timezone
import os
import re
import uuid
from unittest.mock import AsyncMock
from urllib.parse import urlparse

import asyncpg
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from auth import auth_middleware, create_access_token, create_agent_token
from config import config as gateway_config
from core.config import config as core_config
from core.execution.admission import Admission
from core.execution.api import SERVICE_KEY, lane_id, policy_epoch, router as core_routes
from core.execution.executor import Executor
from core.execution.models import Authority, ExecutionError, TOPOLOGY
from core.execution.store import Store
from routers import agent_services, teams


MODEL = {"one": {"label": "Fixture", "backend": "deepseek_v4_flash", "model": "fixture-model",
                 "transport": "text", "enabled": True, "exact_calls": True}}
TEAM = {"schema_version": 1, "id": "configured-team", "topology": TOPOLOGY,
        "roles": {"manager": "one", "worker": "one", "critic": "one"}}


@pytest.fixture
async def pg():
    dsn = os.environ.get("S4B_TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("S4B_TEST_POSTGRES_DSN not set; real Postgres required")
    if urlparse(dsn).hostname not in {None, "localhost", "127.0.0.1", "::1"}:
        pytest.fail("S4b tests permit only local Postgres")
    schema = "s4b_" + uuid.uuid4().hex
    assert re.fullmatch(r"s4b_[a-f0-9]{32}", schema)
    admin = await asyncpg.connect(dsn, timeout=5)
    await admin.execute('CREATE SCHEMA "' + schema + '"')
    pool = None
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6,
                                        server_settings={"search_path": schema}, command_timeout=5)
        # Minimal independent resource ownership tables used by this protocol.
        await pool.execute("CREATE TABLE projects (id UUID PRIMARY KEY,user_id TEXT,is_archived BOOLEAN DEFAULT FALSE);"
                           "CREATE TABLE conversations (id UUID PRIMARY KEY,user_id TEXT,project_id UUID)")
        yield pool
    finally:
        if pool:
            await pool.close()
        await admin.execute('DROP SCHEMA "' + schema + '" CASCADE')
        await admin.close()


@pytest.fixture
async def harness(pg, monkeypatch, tmp_path):
    monkeypatch.setenv("BOB_LANE_ROOT", str(tmp_path))
    monkeypatch.setattr(gateway_config, "BOBCLAW_SECRET", "fixture-execution-vouch-secret")
    monkeypatch.setattr(core_config, "BOBCLAW_SECRET", "fixture-execution-vouch-secret")
    monkeypatch.setattr(gateway_config, "JWT_SECRET", "fixture-jwt-secret-with-at-least-32-characters")
    store = Store(pg, lane_id())
    await store.initialize()
    catalog = AsyncMock(return_value=MODEL)
    service = Admission(store, catalog)
    core = web.Application()
    core[SERVICE_KEY] = service
    core.add_routes(core_routes)
    server = TestServer(core)
    await server.start_server()
    monkeypatch.setattr(gateway_config, "CORE_URL", str(server.make_url("/")).rstrip("/"))
    gateway = web.Application(middlewares=[auth_middleware])
    gateway.add_routes(agent_services.router)
    gateway.add_routes(teams.router)
    from owner_sessions import router as owner_router
    gateway.add_routes(owner_router)
    client = TestClient(TestServer(gateway))
    await client.start_server()
    headers = {"Authorization": "Bearer " + create_access_token("owner-a")}
    actor = Authority(lane_id(), "owner-a", "owner-a", {}, "human", "", policy_epoch(core_config.BOBCLAW_SECRET))
    calls = []

    async def fake(binding, prompt):
        assert binding == MODEL["one"]
        assert "SOURCE-ORIGINAL" in prompt
        calls.append(prompt)
        result = '{"tasks":["inspect SOURCE-ORIGINAL"]}' if "Return only JSON" in prompt else "answer SOURCE-ORIGINAL"
        return {"text": result, "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    class Harness:
        pass
    h = Harness()
    h.store, h.service, h.pg, h.client = store, service, pg, client
    h.catalog, h.headers, h.actor, h.calls, h.fake = catalog, headers, actor, calls, fake
    h.gateway_url = str(client.make_url("/")).rstrip("/")

    async def save(key="save-1", expected=None, definition=None):
        response = await client.post("/teams", headers=headers, json={"contract_version": 2,
            "definition": definition or TEAM, "expected_revision": expected, "request_key": key})
        return response, await response.json()

    async def start(key="start-1", **extra):
        body = {"contract_version": 2, "team": TEAM["id"], "team_revision": 1,
                "task": "Inspect SOURCE-ORIGINAL", "evidence_ids": ["SOURCE-ORIGINAL"],
                "acceptance": "preserve source", "request_key": key,
                "budget": {"start_calls": 100, "wall_mode": "none", "wall_seconds": None}, **extra}
        response = await client.post("/agent-services/runs", headers=headers, json=body)
        return response, await response.json()

    async def get(rid, headers_=None):
        response = await client.get("/agent-services/runs/" + rid, params={"contract_version": 2},
                                    headers=headers_ or headers)
        return response, await response.json()

    h.save, h.start, h.get = save, start, get
    try:
        yield h
    finally:
        await client.close()
        await server.close()


async def execute(h, checkpoint=None, dispatch=None):
    executor = Executor(h.store, h.service, dispatch or h.fake, after_checkpoint=checkpoint, poll=.01)
    row = await h.store.claim(executor.controller)
    if row:
        await executor.execute(row)
    return row


@pytest.mark.asyncio
async def test_real_protocol_save_start_get_list_cancel_two_clients(harness):
    import aiohttp
    h = harness
    response, saved = await h.save()
    assert response.status == 200 and saved["runnable"]
    assert saved["team"]["review_independence"] == "same_model"
    assert (await h.client.get("/teams", params={"contract_version": 2}, headers=h.headers)).status == 200
    response, started = await h.start()
    assert response.status == 202
    rid = started["run"]["id"]
    assert (await h.get(rid))[1]["run"]["status"] == "accepted"
    await execute(h)
    run = (await h.get(rid))[1]["run"]
    assert run["status"] == "completed" and run["calls_used"] == 4
    assert run["effective_call_limit"] == 100 and len(run["receipts"]) == 4
    assert run["evidence_ids"] == ["SOURCE-ORIGINAL"] and not run["independently_verified"]
    # An independent client/token sees the server store, with no shared local ledger.
    async with aiohttp.ClientSession() as second:
        response = await second.get(h.gateway_url + "/agent-services/runs", params={"contract_version": 2},
                                    headers={"Authorization": "Bearer " + create_access_token("owner-a")})
        listed = await response.json()
        assert response.status == 200 and listed["source"] == "server"
        assert [r["id"] for r in listed["items"]] == [rid]
    response = await h.client.post("/agent-services/runs/" + rid + "/cancel", headers=h.headers,
                                  json={"contract_version": 2, "request_key": "cancel-1"})
    assert response.status == 200 and (await response.json())["run"]["status"] == "completed"
    _, queued = await h.start("start-2")
    response = await h.client.post("/agent-services/runs/" + queued["run"]["id"] + "/cancel", headers=h.headers,
                                  json={"contract_version": 2, "request_key": "cancel-queued"})
    assert (await response.json())["run"]["status"] == "cancelled"
    assert len(h.calls) == 4


@pytest.mark.asyncio
async def test_mutation_duplicates_conflicts_and_revision_conflicts(harness):
    h = harness
    _, first = await h.save()
    assert (await h.save())[1]["operation_id"] == first["operation_id"]
    changed = {**TEAM, "state": "draft"}
    assert (await h.save(definition=changed))[0].status == 409
    assert (await h.save("save-new"))[0].status == 409  # create-only
    assert (await h.save("save-new", expected=99))[0].status == 409
    response, revised = await h.save("save-2", expected=1)
    assert response.status == 200 and revised["revision"] == 2
    responses = await asyncio.gather(*(h.start() for _ in range(3)))
    assert all(r.status == 202 for r, _ in responses)
    assert len({d["run"]["id"] for _, d in responses}) == 1
    assert len({d["operation_id"] for _, d in responses}) == 1
    response, conflict = await h.start(task="different task SOURCE-ORIGINAL")
    assert response.status == 409 and conflict["error"]["code"] == "idempotency_conflict"
    assert await h.pg.fetchval("SELECT count(*) FROM agent_runs") == 1
    assert not h.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_after", [0, 1, 2, 3])
async def test_core_restart_after_every_checkpoint_never_replays(harness, crash_after):
    h = harness
    await h.save()
    _, result = await h.start()
    rid = result["run"]["id"]

    async def interrupt(row, ordinal):
        if ordinal == crash_after:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await execute(h, interrupt)
    # New store/service/controller: no request objects/tasks retained.
    h.store = Store(h.pg, lane_id())
    h.service = Admission(h.store, h.catalog)
    await execute(h)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["status"] == "completed" and run["calls_used"] == 4 and len(h.calls) == 4
    assert await h.pg.fetchval("SELECT count(*) FROM agent_attempts") == 4
    if crash_after < 3:
        events = (await h.store.get(h.actor, rid))["events"]
        assert any(e["kind"] == "recovered" for e in events)


@pytest.mark.asyncio
async def test_unknown_dispatch_is_charged_and_never_replayed(harness):
    h = harness
    await h.save()
    _, started = await h.start()
    rid = started["run"]["id"]
    row = await h.store.claim("old-core")
    await h.store.dispatch(row, 0, MODEL["one"], "persisted intent")
    await h.store.release(row)
    assert await h.store.claim("new-core") is None
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["status"] == "paused" and run["reason"] == "unknown_provider_result"
    assert run["calls_used"] == run["unknown_calls"] == 1
    assert not h.calls
    with pytest.raises(ExecutionError, match="stale_controller"):
        await h.store.checkpoint(row, 0, "stale-attempt", "late result", {})


@pytest.mark.asyncio
async def test_cancel_races_completion_and_keeps_terminal_state(harness):
    h = harness
    await h.save()
    _, started = await h.start()
    rid = started["run"]["id"]
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow(binding, prompt):
        entered.set()
        await release.wait()
        return await h.fake(binding, prompt)

    running = asyncio.create_task(execute(h, dispatch=slow))
    await asyncio.wait_for(entered.wait(), 2)
    receipt = await h.store.cancel(h.actor, rid, "cancel-race")
    release.set()
    await asyncio.wait_for(running, 2)
    replay = await h.store.cancel(h.actor, rid, "cancel-race")
    assert receipt == replay
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["status"] == "cancelled" and len(run["receipts"]) == 0
    assert run["calls_used"] == 1
    assert await h.store.claim("later-core") is None


@pytest.mark.asyncio
async def test_deadline_and_authorization_revocation_pause_before_spend(harness):
    h = harness
    await h.save()
    _, started = await h.start()
    rid = started["run"]["id"]
    await h.pg.execute("UPDATE agent_runs SET deadline=clock_timestamp()-interval '1 second' WHERE id=$1", rid)
    await execute(h)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["status"] == "paused" and run["reason"] == "wall_time_expired" and run["calls_used"] == 0
    _, revoked = await h.start("revoked-start")
    await h.pg.execute("UPDATE agent_runs SET authorization_revoked=TRUE WHERE id=$1", revoked["run"]["id"])
    await execute(h)
    run = (await h.store.get(h.actor, revoked["run"]["id"]))["run"]
    assert run["status"] == "paused" and run["reason"] == "authorization_revoked" and run["calls_used"] == 0
    assert not h.calls


@pytest.mark.asyncio
async def test_missing_catalog_and_above_200_fail_before_spend(harness):
    h = harness
    await h.save()
    h.catalog.side_effect = RuntimeError("catalog offline")
    response, result = await h.start()
    assert response.status == 409 and "catalog_unavailable" in result["error"]["detail"]["blocked_reasons"]
    h.catalog.side_effect = None
    response, result = await h.start(budget={"start_calls": 1000, "wall_mode": "none", "wall_seconds": None})
    assert response.status == 409 and result["error"]["code"] == "confirmation_required"
    assert await h.pg.fetchval("SELECT count(*) FROM agent_runs") == 0
    assert not h.calls


@pytest.mark.asyncio
async def test_foreign_lane_owner_project_and_agent_self_grant_denied(harness):
    h = harness
    await h.save()
    _, started = await h.start()
    rid = started["run"]["id"]
    foreign_headers = {"Authorization": "Bearer " + create_access_token("foreign")}
    assert (await h.get(rid, foreign_headers))[0].status == 404
    for auth in (Authority("other-lane", "owner-a", "owner-a", {}, "human"),
                 Authority(lane_id(), "foreign", "foreign", {}, "human")):
        with pytest.raises(ExecutionError) as exc:
            await h.store.get(auth, rid)
        assert exc.value.status == 404
    project = str(uuid.uuid4())
    await h.pg.execute("INSERT INTO projects (id,user_id) VALUES ($1,'foreign')", uuid.UUID(project))
    assert (await h.start("foreign-project", project_id=project))[0].status == 404
    denied = await h.client.get("/agent-services/runs", params={"contract_version": 2, "project_id": project}, headers=h.headers)
    assert denied.status == 404
    agent = {"Authorization": "Bearer " + create_agent_token("owner-a", faces=["researcher"])}
    response = await h.client.post("/agent-services/runs", headers=agent,
                                  json={"contract_version": 2, "budget_grant_id": "self-approved"})
    assert response.status == 422
    assert (await h.get(rid, agent))[0].status == 404
    assert not h.calls


@pytest.mark.asyncio
async def test_two_pumps_fence_and_catalog_revalidation(harness):
    h = harness
    await h.save()
    _, started = await h.start()
    rid = started["run"]["id"]
    first = await h.store.claim("first")
    assert await h.store.claim("second") is None
    await h.store.release(first)
    second = await h.store.claim("second")
    assert second["generation"] > first["generation"]
    with pytest.raises(ExecutionError, match="stale_controller"):
        await h.store.dispatch(first, 0, MODEL["one"], "stale")
    h.catalog.return_value = {"one": {**MODEL["one"], "enabled": False}}
    await Executor(h.store, h.service, h.fake, poll=.01).execute(second)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["status"] == "paused" and run["calls_used"] == 0
