"""3.1 acceptance against disposable real Postgres; no storage substitutes."""
import asyncio
from dataclasses import replace
import uuid

import pytest

from core.execution.models import Authority, ExecutionError, canonical
from core.execution.executor import Executor
from core.execution.call_guard import consume_permit, consume_transport
from tests.test_agent_loop_postgres import pg, loop_h, TEAM, CONTRACT, MODEL, ready_steps, invariants
from tests.test_durable_runs_v2 import harness


def request(key="run-key", calls=400, **extra):
    return {"contract_version": 2, "team": TEAM["id"], "team_revision": 1, "task": "Supplied evidence S1",
            "evidence_ids": ["S1"], "acceptance": CONTRACT, "request_key": key,
            "budget": {"start_calls": calls, "wall_mode": "none", "wall_seconds": None}, **extra}


async def approve(h, proposal, kind="task", key="issue", **limits):
    return await h.store.issue_grant(h.owner, {"proposal_id": proposal["proposal_id"], "proposal_revision": 1,
        "expected_revision": None, "request_key": key, "coverage_kind": kind, "allow_long_runs": True,
        "auto_escalate": True, "call_ceiling": None, "expires_at": None, **limits})


@pytest.mark.asyncio
async def test_intent_grant_start_replays_and_original_actor_preserved(loop_h):
    h = loop_h
    agent = Authority("test-lane", "owner", "agent-one", {"may_touch": ["one/**"]}, "agent", "credential-one")
    await h.service.save(agent, {"definition": {**TEAM, "id": "agent-team"}, "expected_revision": None, "request_key": "agent-team"})
    body = request(team="agent-team")
    args = {"request": body, "request_key": "prepare"}
    proposal = await h.service.prepare(agent, args)
    assert await h.service.prepare(agent, args) == proposal
    assert await h.pg.fetchval("SELECT count(*) FROM agent_runs") == 0
    issued = await approve(h, proposal)
    assert await approve(h, proposal) == issued
    body = {**body, "intent_id": proposal["intent_id"]}
    first = await h.service.start(agent, body)
    second = await h.service.start(agent, body)
    assert first["operation_id"] == second["operation_id"]
    run = first["run"]
    assert run["id"] == proposal["coverage"]["planned_run_id"]
    assert run["actor"] == "agent-one" and run["selected_grant_id"] == issued["grant"]["id"]
    persisted = await h.pg.fetchval("SELECT run_authority FROM agent_runs WHERE id=$1", run["id"])
    assert '"agent-one"' in persisted
    with pytest.raises(ExecutionError, match="idempotency_conflict"):
        await h.service.start(agent, {**body, "task": "changed"})
    with pytest.raises(ExecutionError):
        await h.service.start(replace(agent, actor="agent-two"), {**body, "request_key": "foreign"})
    with pytest.raises(ExecutionError, match="admission_intent_conflict"):
        await h.service.start(agent, {**body, "request_key": "reuse-intent"})
    assert await h.pg.fetchval("SELECT count(*) FROM agent_runs") == 1


@pytest.mark.asyncio
async def test_project_grant_future_task_matching_exclusions_and_pinned_revocation(loop_h):
    h = loop_h
    body = request(project_id="p")
    proposal = await h.service.prepare(h.owner, {"request": body, "request_key": "prepare"})
    issued = await approve(h, proposal, "project")
    # An explicitly reviewed v2 project grant covers different future task text.
    started = await h.service.start(h.owner, request("future", project_id="p", task="A future task S1"))
    rid = started["run"]["id"]
    assert started["run"]["selected_grant_id"] == issued["grant"]["id"]
    for auth, extra in ((h.owner, {"project_id": "other"}),
                        (replace(h.owner, scope={"may_touch": ["other/**"]}), {"project_id": "p"})):
        with pytest.raises(ExecutionError, match="confirmation_required"):
            await h.service.start(auth, request(str(uuid.uuid4()), **extra))
    # Another matching grant cannot replace the selected one after revocation.
    await approve(h, proposal, "project", key="broader", call_ceiling=9000)
    revoked = await h.store.revoke_grant(h.owner, {"grant_id": issued["grant"]["id"], "expected_revision": 1, "request_key": "revoke"})
    assert revoked["affected_runs"] == [{"run_id": rid, "cleanup_pending": False}]
    run = (await h.store.get(h.owner, rid))["run"]
    assert run["status"] == "paused" and run["owner_flag"] and run["selected_grant_id"] == issued["grant"]["id"]
    assert await h.store.claim("new-core") is None
    with pytest.raises(ExecutionError, match="run_not_resumable"):
        await h.store.resume(h.owner, {"run_id": rid, "expected_revision": run["revision"], "request_key": "resume"})


@pytest.mark.asyncio
async def test_revocation_races_charge_without_following_dispatch(loop_h):
    h = loop_h
    body = request(calls=10)
    proposal = await h.service.prepare(h.owner, {"request": body, "request_key": "prepare"})
    issued = await approve(h, proposal)
    await h.service.start(h.owner, {**body, "intent_id": proposal["intent_id"]})
    row = await h.store.claim("core")
    await ready_steps(h, row)
    attempt = await h.store.reserve_attempt(row, 0, MODEL["one"], "S1")
    results = await asyncio.gather(
        h.store.charge_attempt(row, attempt),
        h.store.revoke_grant(h.owner, {"grant_id": issued["grant"]["id"], "expected_revision": 1, "request_key": "revoke"}),
        return_exceptions=True)
    assert not isinstance(results[1], Exception)
    used = (await h.store.get(h.owner, row["id"]))["run"]["calls_used"]
    assert used in {0, 1}
    assert results[1]["affected_runs"][0]["cleanup_pending"] is bool(used)
    with pytest.raises(ExecutionError):
        await h.store.reserve_attempt(row, 1, MODEL["one"], "S1")
    assert (await h.store.get(h.owner, row["id"]))["run"]["calls_used"] == used
    await invariants(h)


@pytest.mark.asyncio
async def test_project_grant_1200_fake_provider_calls(loop_h):
    h = loop_h
    proposal = await h.service.prepare(h.owner, {"request": request(calls=200, project_id="p"), "request_key": "prepare"})
    issued = await approve(h, proposal, "project", call_ceiling=1600)
    rid = await h.start(calls=200, project_id="p", task="Future task under approved project S1")
    row = await h.store.claim("core")
    calls = 0
    async def fake(binding, prompt):
        nonlocal calls
        consume_permit()
        consume_transport()
        calls += 1
        assert await h.pg.fetchval("SELECT consumed FROM agent_budget_accounts WHERE run_id=$1", rid) == calls
        if calls % 2:
            last = calls == 1199
            return {"text": canonical({"tasks": [{"id": "t"+str(calls), "instruction": "work "+str(calls),
                "dependencies": [], "criteria_ids": ["final"] if last else [], "output_kind": "answer" if last else "intermediate"}]})}
        return {"text": canonical({"content": "Conclusion S1", "citations": ["S1"]})}
    await Executor(h.store, h.service, fake, poll=.001).execute(row)
    run = (await h.store.get(h.owner, rid))["run"]
    assert run["status"] == "completed" and run["calls_used"] == calls == 1200
    assert run["selected_grant_id"] == issued["grant"]["id"]
    assert [e["detail"]["new_limit"] for e in run["growth_events"]] == [400, 800, 1600]
    pool = await h.store.get_budget(h.owner, "p")
    assert pool["consumed"] == 1200 and pool["reserved"] == 0 and not pool["configured"]
    await invariants(h)


@pytest.mark.asyncio
async def test_concurrent_hold_remove_disabled_counters_and_reenable(loop_h):
    h = loop_h
    await h.store.set_budget(h.owner, {"project_id": "p", "call_limit": 10, "expected_revision": None, "request_key": "set"})
    results = await asyncio.gather(h.start(calls=4, project_id="p"),
        h.store.remove_budget(h.owner, {"project_id": "p", "expected_revision": 1, "request_key": "remove"}), return_exceptions=True)
    assert isinstance(results[0], str)
    if isinstance(results[1], ExecutionError):
        assert results[1].code == "budget_in_use" and results[1].detail["held"] == 4
    row = await h.store.claim("core")
    await ready_steps(h, row, 1)
    attempt = await h.store.reserve_attempt(row, 0, MODEL["one"], "S1")
    await h.store.charge_attempt(row, attempt)
    await h.store.stop(row, "no_progress", unknown=True)
    pool = await h.store.get_budget(h.owner, "p")
    assert pool["consumed"] == 1 and pool["reserved"] == 0
    if pool["configured"]:
        await h.store.remove_budget(h.owner, {"project_id": "p", "expected_revision": pool["revision"], "request_key": "remove-again"})
    pool = await h.store.get_budget(h.owner, "p")
    await h.start("disabled", calls=4, project_id="p")
    pool = await h.store.get_budget(h.owner, "p")
    assert (pool["consumed"], pool["reserved"], pool["call_limit"]) == (1, 4, None)
    with pytest.raises(ExecutionError, match="below_committed_budget"):
        await h.store.set_budget(h.owner, {"project_id": "p", "call_limit": 4, "expected_revision": pool["revision"], "request_key": "too-low"})
    await invariants(h)


@pytest.mark.asyncio
async def test_deterministic_backfill_from_prepool_runs_preserves_spend(loop_h):
    h = loop_h
    await h.start(calls=4, project_id="p")
    row = await h.store.claim("core")
    await ready_steps(h, row, 1)
    attempt = await h.store.reserve_attempt(row, 0, MODEL["one"], "S1")
    await h.store.charge_attempt(row, attempt)
    await h.pg.execute("DELETE FROM agent_pool_holds; DELETE FROM agent_call_pools; DELETE FROM agent_execution_migrations")
    await h.store.initialize()
    first = await h.store.get_budget(h.owner, "p")
    assert (first["consumed"], first["reserved"]) == (1, 3)
    assert first["counter_coverage"] == "durable_runs_only"
    await h.store.initialize()
    second = await h.store.get_budget(h.owner, "p")
    assert (second["consumed"], second["reserved"]) == (1, 3)


@pytest.mark.asyncio
async def test_resume_above_200_replays_owner_receipt_and_preserves_initiator(loop_h):
    h = loop_h
    rid = await h.start(calls=200)
    row = await h.store.claim("core")
    await h.store.stop(row, "confirmation_required", unknown=False)
    run = (await h.store.get(h.owner, rid))["run"]
    proposal = await h.service.prepare(h.owner, {"run_id": rid, "expected_revision": run["revision"], "request_key": "prepare-resume"})
    issued = await approve(h, proposal)
    run = (await h.store.get(h.owner, rid))["run"]
    args = {"run_id": rid, "expected_revision": run["revision"], "request_key": "resume-key", "budget": {"start_calls": 1200}}
    first = await h.store.resume(h.owner, args)
    assert await h.store.resume(h.owner, args) == first
    run = (await h.store.get(h.owner, rid))["run"]
    assert run["effective_call_limit"] == 1200 and run["actor"] == row["actor"]
    assert run["selected_grant_id"] == issued["grant"]["id"]


@pytest.mark.asyncio
async def test_owner_http_issue_start_resume_budget_and_foreign_resource_denials(harness, monkeypatch):
    h = harness
    from auth import create_portal_token, create_agent_token
    from db import init_db
    from tests.test_owner_sessions import stepup
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "owner-a")
    monkeypatch.setenv("BOB_EXECUTION_BOUNDARY", "worker")
    await init_db()
    await h.save(definition=TEAM)
    portal = {"Authorization": "Bearer "+create_portal_token("owner-a", {"may_touch": ["one/**"]}), "X-Bob-Broker-Link": "full-protocol-link"}
    owner, _ = await stepup(h.client, portal, "budgets.list", {})
    project, foreign, absent = (str(uuid.uuid4()) for _ in range(3))
    await h.pg.execute("INSERT INTO projects(id,user_id) VALUES ($1::uuid,'owner-a'),($2::uuid,'foreign')", uuid.UUID(project), uuid.UUID(foreign))
    body = request(project_id=project)
    response = await h.client.post("/agent-services/grant-proposals", headers=portal,
                                   json={"contract_version": 2, "request": body, "request_key": "prepare"})
    assert response.status == 200, await response.text()
    proposal = await response.json()
    issue = {"contract_version": 2, "proposal_id": proposal["proposal_id"], "proposal_revision": 1, "expected_revision": None,
             "request_key": "issue-lost", "coverage_kind": "task", "allow_long_runs": True,
             "auto_escalate": True, "call_ceiling": None, "expires_at": None}
    for denied in (portal, h.headers, {"Authorization": "Bearer "+create_agent_token("owner-a", faces=["researcher"])}):
        assert (await h.client.post("/agent-services/grants", headers=denied, json=issue)).status in {403,404}
    response = await h.client.post("/agent-services/grants", headers=owner, json=issue)
    assert response.status == 200, await response.text()
    issued = await response.json()
    response = await h.client.post("/agent-services/grants", headers=owner, json=issue)
    assert await response.json() == issued
    start = {**body, "intent_id": proposal["intent_id"]}
    response = await h.client.post("/agent-services/runs", headers=portal, json=start)
    assert response.status == 202, await response.text()
    started = await response.json()
    response = await h.client.post("/agent-services/runs", headers=portal, json=start)
    assert (await response.json())["operation_id"] == started["operation_id"]
    row = await h.store.claim("core")
    assert row["run_authority"]["token_type"] == "portal" and row["run_authority"]["scope"] == {"may_touch": ["one/**"]}
    await h.store.stop(row, "confirmation_required", unknown=False)
    run = (await h.store.get(h.actor, row["id"]))["run"]
    resume = {"contract_version": 2, "expected_revision": run["revision"], "request_key": "resume-lost", "budget": {"start_calls": 1200}}
    path = "/agent-services/runs/"+row["id"]+"/resume"
    first = await h.client.post(path, headers=owner, json=resume)
    assert first.status == 200, await first.text()
    resumed = await first.json()
    assert resumed["run"]["effective_call_limit"] == 1200 and resumed["run"]["selected_grant_id"] == issued["grant"]["id"]
    assert (await (await h.client.post(path, headers=owner, json=resume)).json())["operation_id"] == resumed["operation_id"]
    for resource in (foreign, absent):
        response = await h.client.get("/agent-services/budgets/projects/"+resource, headers=owner, params={"contract_version": 2})
        assert response.status == 404
    response = await h.client.get("/agent-services/budgets/projects/"+project, headers=owner, params={"contract_version": 2})
    pool = await response.json()
    assert pool["consumed"] == 0 and pool["reserved"] == 1200 and pool["call_limit"] is None
    # Setting below held commitments is rejected even for an unenforced pool.
    response = await h.client.post("/agent-services/budgets", headers=owner, json={"contract_version": 2, "project_id": project,
        "call_limit": 1000, "expected_revision": pool["revision"], "request_key": "below"})
    assert response.status == 409
    response = await h.client.post("/agent-services/budgets/projects/"+project+"/remove", headers=owner,
        json={"contract_version": 2, "expected_revision": pool["revision"], "request_key": "remove"})
    assert response.status == 409 and (await response.json())["error"]["code"] == "budget_in_use"
    assert not h.calls  # no inference at proposal, issuance, start or resume


@pytest.mark.asyncio
async def test_agent_future_project_tasks_require_exact_owner_approved_boundary(harness, monkeypatch):
    h = harness
    from auth import create_portal_token, create_agent_token
    from db import init_db
    from tests.test_owner_sessions import stepup
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "owner-a")
    monkeypatch.setenv("BOB_EXECUTION_BOUNDARY", "worker")
    await init_db()
    project = str(uuid.uuid4())
    await h.pg.execute("INSERT INTO projects(id,user_id) VALUES ($1,'owner-a')", uuid.UUID(project))
    token = create_agent_token("owner-a", scope={"may_touch": ["one/**"]}, faces=["researcher"])
    agent = {"Authorization": "Bearer "+token}
    definition = {**TEAM, "id": "agent-project-team"}
    response = await h.client.post("/teams", headers=agent, json={"contract_version": 2, "definition": definition,
                                                               "expected_revision": None, "request_key": "team"})
    assert response.status == 200, await response.text()
    body = request(calls=10, project_id=project, team=definition["id"])
    # No project access is inferred from the token's paths, even below 200.
    response = await h.client.post("/agent-services/runs", headers=agent, json=body)
    assert response.status == 404
    response = await h.client.post("/agent-services/grant-proposals", headers=agent,
                                   json={"contract_version": 2, "request": body, "request_key": "prepare"})
    assert response.status == 200, await response.text()
    proposal = await response.json()
    owner, _ = await stepup(h.client, {"Authorization": "Bearer "+create_portal_token("owner-a", {}),
                                       "X-Bob-Broker-Link": "agent-project-review"}, "grants.issue",
                             {"proposal_id": proposal["proposal_id"]})
    response = await h.client.post("/agent-services/grants", headers=owner, json={"contract_version": 2,
        "proposal_id": proposal["proposal_id"], "proposal_revision": 1, "expected_revision": None, "request_key": "issue",
        "coverage_kind": "project", "allow_long_runs": True, "auto_escalate": False, "call_ceiling": 1200, "expires_at": None})
    assert response.status == 200, await response.text()
    started = await h.client.post("/agent-services/runs", headers=agent,
        json={**body, "request_key": "future", "task": "A future task S1", "budget": {"start_calls": 1200, "wall_mode": "none", "wall_seconds": None}})
    assert started.status == 202, await started.text()
    run = (await started.json())["run"]
    row = await h.store.claim("core")
    assert row["actor"] == row["run_authority"]["credential_id"] and row["run_authority"]["token_type"] == "agent"
    assert row["run_authority"]["scope"] == {"may_touch": ["one/**"]}
    response = await h.client.get("/agent-services/runs/"+run["id"], headers=agent, params={"contract_version": 2})
    assert response.status == 200
    other = {"Authorization": "Bearer "+create_agent_token("owner-a", scope={"may_touch": ["one/**"]}, faces=["researcher"])}
    response = await h.client.get("/agent-services/runs/"+run["id"], headers=other, params={"contract_version": 2})
    assert response.status == 404
    # A second credential with the same paths still has a different original actor.
    response = await h.client.post("/agent-services/runs", headers=other, json={**body, "request_key": "foreign"})
    assert response.status == 404
