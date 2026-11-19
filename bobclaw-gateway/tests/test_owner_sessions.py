"""Real temporary SQLite and HTTP; injected time, synthetic factors, no providers."""
import asyncio
from dataclasses import replace
import json
import time
from types import SimpleNamespace

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pyotp
import pytest

from auth import auth_middleware, create_access_token, create_agent_token, create_portal_token
from config import config
from core.execution.api import lane_id, policy_epoch
from core.execution.models import Authority, ExecutionError
from core.execution.owner import require_owner
from db import init_db, _sqlite_connection
import owner_sessions as owners
from routers import agent_services, capabilities


async def stepup(client, headers, operation="runs.resume", resource=None, code=None):
    body = {"operation": operation, "resource": {"run_id": "run-ref"} if resource is None else resource}
    response = await client.post("/auth/owner/begin", headers=headers, json=body)
    assert response.status == 200, await response.text()
    challenge = await response.json()
    response = await client.post("/auth/owner/confirm", headers=headers, json={"challenge_id": challenge["challenge_id"],
                                  "password": config.BOBCLAW_PASSWORD, "totp": code or pyotp.TOTP(config.TOTP_SECRET).at(owners.clock())})
    assert response.status == 200, await response.text()
    result = await response.json()
    import jwt
    claims = jwt.decode(headers["Authorization"][7:], options={"verify_signature": False})
    return {"Authorization": "Bearer " + result["owner_bearer"],
            "X-Bob-Broker-Link": headers["X-Bob-Broker-Link"], "X-Bob-Portal-Credential": claims["jti"]}, result


@pytest.fixture
async def owner_h(monkeypatch, tmp_path):
    monkeypatch.setenv("GATEWAY_SQLITE_PATH", str(tmp_path / "owner-gateway.db"))
    monkeypatch.delenv("BOB_OWNER_WORK_APPROVAL", raising=False)
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "owner")
    monkeypatch.setenv("BOB_EXECUTION_BOUNDARY", "worker")
    monkeypatch.setattr(config, "BOBCLAW_SECRET", "fixture-owner-vouch-secret")
    monkeypatch.setattr(config, "BOBCLAW_PASSWORD", "fixture-owner-password")
    monkeypatch.setattr(config, "TOTP_SECRET", "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP")
    monkeypatch.setattr(config, "JWT_SECRET", "fixture-owner-jwt-secret-at-least-32-characters")
    now = [int(time.time()/30)*30+5]
    monkeypatch.setattr(owners, "clock", lambda: now[0])
    await init_db()
    hits = []

    async def core(request):
        payload = await request.json()
        hits.append(payload)
        from core.permissions import verify_execution_vouch
        assert verify_execution_vouch(payload["envelope"], payload["vouch"], config.BOBCLAW_SECRET)
        return web.json_response({"summary": "authoritative fixture", "revision": 1, "storage_available": True, "execution_enabled": True})

    server_app = web.Application()
    server_app.router.add_post("/api/execution/{op}", core)
    server = TestServer(server_app)
    await server.start_server()
    monkeypatch.setattr(config, "CORE_URL", str(server.make_url("/")).rstrip("/"))

    async def new_client():
        app = web.Application(middlewares=[auth_middleware])
        app.add_routes(owners.router)
        from routers.tool_jobs import router as jobs_router
        app.add_routes(jobs_router)
        from routers.work import router as work_router
        app.add_routes(work_router)
        app.add_routes(agent_services.router)
        app.add_routes(capabilities.router)
        app.router.add_get("/health", lambda r: web.json_response({"ok": True}))
        app.router.add_post("/auth/agent-token", lambda r: web.json_response({"bad": True}))
        client = TestClient(TestServer(app))
        await client.start_server()
        return client

    client = await new_client()
    token = create_portal_token("owner", {"may_touch": ["one/**"]})
    headers = {"Authorization": "Bearer " + token, "X-Bob-Broker-Link": "fixture-link-nonce-256-bit-placeholder"}
    try:
        yield SimpleNamespace(client=client, headers=headers, now=now, hits=hits, new_client=new_client)
    finally:
        await client.close()
        await server.close()


async def work_challenge(h, operation="work.workspace.create", resource=None):
    response = await h.client.post("/auth/owner/begin", headers=h.headers,
        json={"operation": operation, "resource": resource if resource is not None else {"project_id": "project-ref"}})
    assert response.status == 200, await response.text()
    return await response.json()


@pytest.mark.asyncio
async def test_signed_in_work_confirmation_is_bound_one_use_and_narrow(owner_h, monkeypatch):
    h = owner_h
    monkeypatch.setenv("BOB_OWNER_WORK_APPROVAL", "signed_in_user")
    challenge = await work_challenge(h)
    assert challenge["confirmation_mode"] == "signed_in_user"
    body = {"challenge_id": challenge["challenge_id"], "confirmation_mode": "signed_in_user"}
    response = await h.client.post("/auth/owner/confirm", headers=h.headers, json=body)
    assert response.status == 200, await response.text()
    result = await response.json()
    operations = result["owner_session"]["allowed_operations"]
    assert {"work.workspace.create", "work.publish", "grants.issue"} <= set(operations)
    assert "secrets.set" in operations
    assert not {"policy.get", "policy.set"} & set(operations)
    assert result["owner_session"]["expires_at"] == h.now[0] + 300
    import jwt
    portal_claims = jwt.decode(h.headers["Authorization"][7:], options={"verify_signature": False})
    owner = {"Authorization": "Bearer " + result["owner_bearer"],
             "X-Bob-Broker-Link": h.headers["X-Bob-Broker-Link"],
             "X-Bob-Portal-Credential": portal_claims["jti"]}
    response = await h.client.post("/agent-services/runs/run-ref/resume", headers=owner,
        json={"contract_version": 2, "expected_revision": 9, "request_key": "signed-in-resume"})
    assert response.status == 200, await response.text()
    authority = Authority(**h.hits[-1]["envelope"]["authority"])
    await require_owner(authority, "runs.resume")
    for operation in ("policy.get", "policy.set"):
        with pytest.raises(ExecutionError):
            await require_owner(authority, operation)
    assert (await h.client.post("/auth/owner/confirm", headers=h.headers, json=body)).status == 401
    h.now[0] += 301
    fresh = await work_challenge(h)
    renewed = await h.client.post("/auth/owner/confirm", headers=h.headers,
        json={"challenge_id": fresh["challenge_id"], "confirmation_mode": "signed_in_user"})
    assert renewed.status == 200, await renewed.text()
    async with _sqlite_connection() as db:
        async with db.execute("SELECT COUNT(*) FROM totp_timesteps") as cursor:
            assert (await cursor.fetchone())[0] == 0


@pytest.mark.asyncio
async def test_signed_in_confirmation_cannot_select_policy_or_cross_link(owner_h, monkeypatch):
    h = owner_h
    challenge = await work_challenge(h)
    assert challenge["confirmation_mode"] == "password_totp"
    body = {"challenge_id": challenge["challenge_id"], "confirmation_mode": "signed_in_user"}
    assert (await h.client.post("/auth/owner/confirm", headers=h.headers, json=body)).status == 403
    monkeypatch.setenv("BOB_OWNER_WORK_APPROVAL", "signed_in_user")
    foreign = {**h.headers, "X-Bob-Broker-Link": "different-desktop-link"}
    assert (await h.client.post("/auth/owner/confirm", headers=foreign, json=body)).status == 401
    assert (await h.client.post("/auth/owner/confirm", headers={**h.headers, "Authorization": "Bearer " + create_agent_token("owner")}, json=body)).status == 404


@pytest.mark.asyncio
async def test_signed_in_confirmation_covers_storing_a_provider_key(owner_h, monkeypatch):
    h = owner_h
    from routers import settings
    monkeypatch.setattr(settings, "capabilities", lambda: {"secrets.set": {"available": True}})
    # Without the root-configured desktop policy a key still needs both factors.
    challenge = await work_challenge(h, "secrets.set", {"provider": "deepseek"})
    assert challenge["confirmation_mode"] == "password_totp"
    response = await h.client.post("/auth/owner/confirm", headers=h.headers,
        json={"challenge_id": challenge["challenge_id"], "confirmation_mode": "signed_in_user"})
    assert response.status == 403
    monkeypatch.setenv("BOB_OWNER_WORK_APPROVAL", "signed_in_user")
    challenge = await work_challenge(h, "secrets.set", {"provider": "deepseek"})
    assert challenge["confirmation_mode"] == "signed_in_user"
    response = await h.client.post("/auth/owner/confirm", headers=h.headers,
        json={"challenge_id": challenge["challenge_id"], "confirmation_mode": "signed_in_user"})
    assert response.status == 200, await response.text()
    operations = (await response.json())["owner_session"]["allowed_operations"]
    assert "secrets.set" in operations
    assert not {"policy.get", "policy.set"} & set(operations)


@pytest.mark.asyncio
async def test_signed_in_confirmation_keeps_security_changes_protected(owner_h, monkeypatch):
    h = owner_h
    monkeypatch.setenv("BOB_OWNER_WORK_APPROVAL", "signed_in_user")
    from routers import settings
    monkeypatch.setattr(settings, "capabilities", lambda: {"policy.get": {"available": True}})
    challenge = await work_challenge(h, "policy.get", {})
    assert challenge["confirmation_mode"] == "password_totp"
    response = await h.client.post("/auth/owner/confirm", headers=h.headers,
        json={"challenge_id": challenge["challenge_id"], "confirmation_mode": "signed_in_user"})
    assert response.status == 403
    for extra in ({"password": "unused"}, {"totp": "123456"}, {"confirmation_mode": "anything"}):
        response = await h.client.post("/auth/owner/confirm", headers=h.headers,
            json={"challenge_id": challenge["challenge_id"], "confirmation_mode": "signed_in_user", **extra})
        assert response.status == 422


@pytest.mark.asyncio
async def test_owner_vouch_provenance_route_audience_and_original_portal_remains(owner_h):
    h = owner_h
    owner, result = await stepup(h.client, h.headers)
    for path in ("/health", "/auth/agent-token", "/not-a-route", "/agent-services/runs"):
        response = await h.client.get(path, headers=owner)
        assert response.status == 403
    response = await h.client.post("/agent-services/runs/run-ref/resume", headers=owner,
                                   json={"contract_version": 2, "expected_revision": 9, "request_key": "lost-reply"})
    assert response.status == 200
    envelope = h.hits[-1]["envelope"]
    auth = Authority(**envelope["authority"])
    assert auth.session_kind == "owner_stepup" and auth.credential_epoch == 1
    assert auth.portal_credential_id == owner["X-Bob-Portal-Credential"]
    assert auth.scope == {} and auth.actor == auth.owner == "owner"
    await require_owner(auth, "runs.resume")
    for changed in (replace(auth, credential_epoch=2), replace(auth, actor="admin"),
                    replace(auth, lane="foreign"), replace(auth, portal_credential_id="other"),
                    replace(auth, broker_link_nonce="new-link"), replace(auth, session_kind="")):
        with pytest.raises(ExecutionError):
            await require_owner(changed, "runs.resume")
    assert result["owner_session"]["expires_at"] == h.now[0] + 300
    assert (await h.client.get("/health", headers=h.headers)).status == 200
    async with _sqlite_connection() as db:
        async with db.execute("SELECT token_hash FROM owner_sessions") as c:
            hashes = [row[0] for row in await c.fetchall()]
    assert result["owner_bearer"] not in json.dumps(hashes)


@pytest.mark.asyncio
async def test_gateway_restart_link_binding_absolute_expiry_and_lock(owner_h):
    h = owner_h
    owner, result = await stepup(h.client, h.headers)
    fresh = await h.new_client()
    try:
        response = await fresh.get("/auth/owner/status", headers=owner)
        assert response.status == 200 and (await response.json())["owner_session"] == result["owner_session"]
        assert (await fresh.get("/auth/owner/status", headers={**owner, "X-Bob-Broker-Link": "broker-restarted"})).status == 403
        h.now[0] += 299
        assert (await fresh.get("/auth/owner/status", headers=owner)).status == 200
        h.now[0] += 1
        assert (await fresh.get("/auth/owner/status", headers=owner)).status == 403
    finally:
        await fresh.close()
    h.now[0] += 31
    owner, _ = await stepup(h.client, h.headers)
    assert (await h.client.post("/auth/owner/lock", headers=owner, json={})).status == 200
    assert (await h.client.get("/auth/owner/status", headers=owner)).status == 403


@pytest.mark.asyncio
async def test_concurrent_totp_single_winner_and_limiter_survives_restart(owner_h):
    h = owner_h
    challenges = []
    for _ in range(2):
        response = await h.client.post("/auth/owner/begin", headers=h.headers,
                                       json={"operation": "runs.resume", "resource": {"run_id": "run-ref"}})
        challenges.append((await response.json())["challenge_id"])
    async def confirm(challenge):
        return await h.client.post("/auth/owner/confirm", headers=h.headers, json={"challenge_id": challenge,
            "password": config.BOBCLAW_PASSWORD, "totp": pyotp.TOTP(config.TOTP_SECRET).at(h.now[0])})
    responses = await asyncio.gather(*(confirm(c) for c in challenges))
    assert sorted(r.status for r in responses) == [200, 401]
    await init_db()  # process replacement retains attempts and replay high-water mark
    fresh = await h.new_client()
    try:
        response = await fresh.post("/auth/owner/confirm", headers={**h.headers, "X-Bob-Broker-Link": "rotated"},
                                     json={"challenge_id": challenges[1], "password": "bad", "totp": "000000"})
        assert response.status == 429 and int(response.headers["Retry-After"]) >= 1
    finally:
        await fresh.close()
    h.now[0] += 2
    # Fresh challenge still cannot reuse the consumed timestep.
    response = await h.client.post("/auth/owner/begin", headers=h.headers,
                                   json={"operation": "runs.resume", "resource": {"run_id": "run-ref"}})
    assert (await confirm((await response.json())["challenge_id"])).status == 401


@pytest.mark.asyncio
async def test_password_only_agent_plain_human_and_foreign_subject_cannot_step_up(owner_h, monkeypatch):
    h = owner_h
    for token in (create_access_token("owner"), create_access_token("admin"), create_agent_token("owner"), create_portal_token("foreign", {})):
        headers = {**h.headers, "Authorization": "Bearer " + token}
        response = await h.client.post("/auth/owner/begin", headers=headers, json={"operation": "runs.resume", "resource": {"run_id": "run-ref"}})
        assert response.status == 404
        response = await h.client.post("/auth/owner/confirm", headers=headers, json={})
        assert response.status == 404
    monkeypatch.delenv("BOB_DEFAULT_OWNER")
    assert (await h.client.post("/auth/owner/begin", headers=h.headers, json={})).status == 404
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "owner")
    monkeypatch.setenv("BOB_EXECUTION_BOUNDARY", "legacy")
    assert (await h.client.post("/auth/owner/begin", headers=h.headers, json={})).status == 404
    assert not h.hits


@pytest.mark.asyncio
async def test_epoch_invalidates_sessions_and_no_legacy_admin_fallback(owner_h):
    h = owner_h
    owner, _ = await stepup(h.client, h.headers)
    async with _sqlite_connection() as db:
        await db.execute("UPDATE owner_epochs SET epoch=2")
        await db.commit()
    assert (await h.client.get("/auth/owner/status", headers=owner)).status == 403


@pytest.mark.asyncio
async def test_operation_capabilities_explain_boundary_and_mapping(owner_h, monkeypatch):
    h = owner_h
    async def catalog():
        response = await h.client.get("/capabilities", headers=h.headers, params={"operations": 1, "contract_version": 2})
        assert response.status == 200
        return (await response.json())["operations"]
    operations = await catalog()
    assert operations["runs.resume"]["owner_session_required"]
    assert operations["grants.prepare"]["available"] and not operations["grants.prepare"]["owner_session_required"]
    assert not operations["grants.issue"]["job_grants_available"]
    monkeypatch.delenv("BOB_DEFAULT_OWNER")
    assert (await catalog())["owner.begin"]["reason"] == "explicit_lane_owner_required"


@pytest.mark.asyncio
async def test_core_rejects_forged_and_stale_epoch_vouches_before_storage(owner_h, monkeypatch):
    h = owner_h
    owner, _ = await stepup(h.client, h.headers)
    await h.client.post("/agent-services/runs/run-ref/resume", headers=owner,
                        json={"contract_version": 2, "expected_revision": 1, "request_key": "proof"})
    authority = h.hits[-1]["envelope"]["authority"]
    from core.config import config as core_config
    from core.execution.api import router as core_routes
    from core.permissions import execution_vouch
    monkeypatch.setattr(core_config, "BOBCLAW_SECRET", config.BOBCLAW_SECRET)
    app = web.Application()
    app.add_routes(core_routes)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        async def send(auth, forged=False):
            now = int(time.time())
            envelope = {"operation": "grants.list", "args": {}, "authority": auth, "iat": now, "exp": now+30}
            return await client.post("/api/execution/grants.list", json={"envelope": envelope,
                "vouch": "0"*64 if forged else execution_vouch(envelope, config.BOBCLAW_SECRET)})
        response = await send(authority, forged=True)
        assert response.status == 403 and (await response.json())["error"]["code"] == "invalid_execution_vouch"
        response = await send({**authority, "credential_epoch": 2})
        assert response.status == 403 and (await response.json())["error"]["code"] == "credential_epoch_changed"
        response = await send({**authority, "session_kind": ""})
        assert response.status == 404
        # Valid provenance reaches the deliberately absent store; no fake DB oracle.
        response = await send(authority)
        assert response.status == 503 and (await response.json())["error"]["code"] == "execution_storage_unavailable"
    finally:
        await client.close()
