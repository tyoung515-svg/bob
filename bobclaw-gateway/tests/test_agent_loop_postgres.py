"""Real Postgres accounting/recovery tests. No memory/SQLite store substitute.

Uses the 4.1 temporary-schema fixture and S4B_TEST_POSTGRES_DSN (local only).
Fake providers only; run with the gateway tests and core on PYTHONPATH.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_durable_runs_v2 import pg, harness, MODEL
from auth import create_agent_token, create_portal_token
from core.execution.admission import Admission
from core.execution.budgets import task_digest
from core.execution.call_guard import consume_permit, consume_transport
from core.execution.executor import Executor
from core.execution.models import Authority, ExecutionError, LOOP_TOPOLOGY, canonical, digest
from core.execution.store import Store

CONTRACT = {"criteria": [{"id": "final", "output_kind": "answer", "required": True,
                          "checks": {"required_sections": ["Conclusion"], "require_citations": True}}]}
TEAM = {"schema_version": 1, "id": "loop-team", "topology": LOOP_TOPOLOGY,
        "roles": {r: "one" for r in ("manager", "worker", "critic")}}


@pytest.fixture
async def loop_h(pg):
    store = Store(pg, "test-lane")
    await store.initialize()
    owner = Authority("test-lane", "owner", "owner", {}, "human")
    service = Admission(store, AsyncMock(return_value=MODEL))
    await service.save(owner, {"definition": TEAM, "expected_revision": None, "request_key": "save"})
    h = SimpleNamespace(pg=pg, store=store, owner=owner, service=service)

    async def start(key="start", calls=10, **extra):
        request = {"contract_version": 2, "team": TEAM["id"], "team_revision": 1,
                   "task": "Supplied evidence S1", "evidence_ids": ["S1"], "acceptance": CONTRACT,
                   "request_key": key, "budget": {"start_calls": calls, "wall_mode": "none", "wall_seconds": None}, **extra}
        result = await service.start(owner, request)
        return result["run"]["id"]
    h.start = start
    return h


async def grant(h, row, ceiling=None, long=True, auto=True):
    await h.pg.execute(
        "INSERT INTO agent_execution_grants(lane,owner,id,run_id,authority_boundary,task_digest,team_digest,models_digest,allow_long_runs,auto_escalate,call_ceiling) "
        "VALUES ($1,$2,$3,$3,$4,$5,$6,$7,$8,$9,$10)",
        row["lane"], row["owner"], row["id"], row["boundary"], task_digest(row["request"]),
        digest(row["snapshot"]["team"]), digest(row["snapshot"]["models"]), long, auto, ceiling)


async def exhaust(h, row):
    """Fixture for deterministic exhaustion evaluation, without 800 fake calls.

    Actual reserve/charge/settle order is exercised by the race and 1200-call cases.
    """
    async with h.pg.acquire() as conn, conn.transaction():
        await conn.execute("UPDATE agent_budget_accounts SET consumed=call_limit WHERE run_id=$1", row["id"])
        await conn.execute("UPDATE agent_budget_allocations SET consumed=allowance,reserved=0,delegated=0 WHERE run_id=$1 AND id='root'", row["id"])
        holds = await conn.fetch("SELECT * FROM agent_pool_holds WHERE run_id=$1", row["id"])
        for hold in holds:
            await conn.execute("UPDATE agent_call_pools SET consumed=consumed+$5,reserved=reserved-$5 WHERE lane=$1 AND owner=$2 AND kind=$3 AND id=$4", hold["lane"], hold["owner"], hold["kind"], hold["pool_id"], hold["remaining"])
        await conn.execute("UPDATE agent_pool_holds SET remaining=0 WHERE run_id=$1", row["id"])


async def evaluate(h, row):
    revision = (await h.store.get(h.owner, row["id"]))["run"]["revision"]
    return await h.store.evaluate_growth(row, revision)


async def ready_steps(h, row, n=2):
    for ordinal in range(n):
        spec = {"phase": "task", "cycle": 1, "id": str(ordinal), "instruction": "evidence",
                "dependencies": [], "criteria_ids": [], "output_kind": "intermediate"}
        await h.pg.execute("INSERT INTO agent_run_steps(lane,owner,run_id,ordinal,role,spec) VALUES ($1,$2,$3,$4,'worker',$5::jsonb)",
                           row["lane"], row["owner"], row["id"], ordinal, canonical(spec))


async def invariants(h):
    assert await h.pg.fetchval("SELECT count(*) FROM agent_call_pools WHERE consumed+reserved>call_limit") == 0
    assert await h.pg.fetchval("SELECT count(*) FROM agent_budget_allocations WHERE consumed+reserved+delegated>allowance") == 0
    assert await h.pg.fetchval("SELECT count(*) FROM agent_budget_accounts a JOIN agent_budget_allocations b ON (a.lane,a.owner,a.run_id)=(b.lane,b.owner,b.run_id) WHERE b.id='root' AND (a.consumed<>b.consumed OR b.consumed+b.reserved+b.delegated>a.call_limit)") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("start,expected", [(10,50),(30,50),(50,100),(100,200)])
async def test_growth_uses_next_total_and_one_revision_wins(loop_h, start, expected):
    h = loop_h
    rid = await h.start(calls=start)
    row = await h.store.claim("core")
    await exhaust(h, row)
    revision = (await h.store.get(h.owner, rid))["run"]["revision"]
    results = await asyncio.gather(*(h.store.evaluate_growth(row, revision) for _ in range(2)), return_exceptions=True)
    assert results.count("grown") == 1
    assert any(isinstance(r, ExecutionError) and r.code == "revision_conflict" for r in results)
    run = (await h.store.get(h.owner, rid))["run"]
    assert run["effective_call_limit"] == expected and run["calls_used"] == start
    assert [(e["detail"]["old_limit"],e["detail"]["new_limit"]) for e in run["growth_events"]] == [(start,expected)]
    await invariants(h)


@pytest.mark.asyncio
async def test_200_pause_and_granted_doubling_owner_ceiling(loop_h):
    h = loop_h
    rid = await h.start(calls=200)
    row = await h.store.claim("core")
    await exhaust(h, row)
    assert await evaluate(h, row) == "confirmation_required"
    run = (await h.store.get(h.owner, rid))["run"]
    assert run["owner_flag"] and run["status"] == "paused"
    await grant(h, row, ceiling=1600)
    receipt = await h.store.resume(h.owner, {"run_id": rid, "expected_revision": run["revision"],
                                           "request_key": "resume", "budget": {"start_calls": 400}})
    assert receipt["run_id"] == rid
    row = await h.store.claim("new-core")
    for target in (800,1600):
        await exhaust(h, row)
        assert await evaluate(h, row) == "grown"
        assert (await h.store.get(h.owner, rid))["run"]["effective_call_limit"] == target
    await exhaust(h, row)
    assert await evaluate(h, row) == "owner_ceiling"
    await invariants(h)


@pytest.mark.asyncio
async def test_no_output_1200_real_charged_calls_without_truncation(loop_h):
    h = loop_h
    rid = await h.start(calls=200)
    row = await h.store.claim("core")
    await grant(h, row)
    count = 0
    current = {}

    async def fake(binding, prompt):
        nonlocal count, current
        consume_permit()
        consume_transport()
        # The database charge is visible before every model transport.
        charged = await h.pg.fetchval("SELECT consumed FROM agent_budget_accounts WHERE run_id=$1", rid)
        assert charged == count + 1
        count += 1
        if count % 2:
            last = count == 1199
            current = {"id": "t" + str(count), "instruction": "bounded iteration " + str(count), "dependencies": [],
                       "output_kind": "answer" if last else "intermediate", "criteria_ids": ["final"] if last else []}
            return {"text": canonical({"tasks": [current]})}
        return {"text": canonical({"content": "Conclusion from S1", "citations": ["S1"]})}

    await Executor(h.store, h.service, fake, poll=.001).execute(row)
    run = (await h.store.get(h.owner, rid))["run"]
    assert run["status"] == "completed" and run["calls_used"] == count == 1200
    assert run["accepted_output_count"] == 1 and run["effective_call_limit"] == 1600
    assert [e["detail"]["new_limit"] for e in run["growth_events"]] == [400,800,1600]
    assert await h.pg.fetchval("SELECT count(*) FROM agent_attempts WHERE run_id=$1 AND state='completed'", rid) == 1200
    cursor, receipts = 0, []
    while cursor is not None:
        page = await h.store.get(h.owner, rid, receipt_cursor=cursor)
        receipts.extend(page["receipts"])
        cursor = page["next_receipt_cursor"]
    assert len(receipts) == 1200
    await invariants(h)


@pytest.mark.asyncio
async def test_nested_and_concurrent_last_credit_reserve_charge_refund(loop_h):
    h = loop_h
    await h.start(calls=2)
    row = await h.store.claim("core")
    await ready_steps(h, row)
    await h.store.suballocate(row, "root", "child", 1)
    await h.store.suballocate(row, "child", "grandchild", 1)
    results = await asyncio.gather(*(h.store.reserve_attempt(row, n, MODEL["one"], "source", "grandchild") for n in range(2)), return_exceptions=True)
    attempts = [r for r in results if isinstance(r, str)]
    assert len(attempts) == 1
    assert any(isinstance(r, ExecutionError) and r.code == "call_budget_exhausted" for r in results)
    assert (await h.store.get(h.owner, row["id"]))["run"]["reserved_calls"] == 1
    await h.store.charge_attempt(row, attempts[0])
    with pytest.raises(ExecutionError, match="unreserved_model_call"):
        await h.store.charge_attempt(row, attempts[0])
    await invariants(h)
    assert (await h.store.get(h.owner, row["id"]))["run"]["calls_used"] == 1


@pytest.mark.asyncio
async def test_two_runs_project_sprint_pool_last_credit_and_rollback(loop_h):
    h = loop_h
    await h.store.set_budget(h.owner, {"project_id": "p", "call_limit": 20, "expected_revision": None, "request_key": "pool"})
    await h.pg.execute("INSERT INTO agent_call_pools(lane,owner,kind,id,call_limit) VALUES ('test-lane','owner','sprint','s',4)")
    # Sprint admission is supported by storage, but explicitly unavailable to API
    # callers until the sprint resource service exists.
    team = await h.store.team(h.owner, TEAM["id"])
    snapshot = {"team": team, "models": MODEL, "review_independence": "same_model"}
    def request(key):
        return {"team": TEAM["id"], "team_revision": 1, "task": "evidence", "request_key": key,
                "project_id": "p", "sprint_id": "s", "acceptance": CONTRACT,
                "budget": {"start_calls": 2, "wall_mode": "none", "wall_seconds": None}}
    await asyncio.gather(*(h.store.start(h.owner, request(str(n)), snapshot) for n in range(2)))
    before = [dict(r) for r in await h.pg.fetch("SELECT * FROM agent_call_pools ORDER BY kind")]
    with pytest.raises(ExecutionError, match="pool_shortfall") as exc:
        await h.store.start(h.owner, request("third"), snapshot)
    assert exc.value.detail["remaining"] == 0
    assert [dict(r) for r in await h.pg.fetch("SELECT * FROM agent_call_pools ORDER BY kind")] == before
    assert await h.pg.fetchval("SELECT count(*) FROM agent_runs") == 2
    rows = [await h.store.claim("c1"), await h.store.claim("c2")]
    for row in rows:
        await ready_steps(h, row)
    for ordinal in (0,1):
        attempts = await asyncio.gather(*(h.store.reserve_attempt(row, ordinal, MODEL["one"], "source") for row in rows))
        await asyncio.gather(*(h.store.charge_attempt(row, attempt) for row, attempt in zip(rows,attempts)))
    await invariants(h)
    assert await h.pg.fetchval("SELECT consumed FROM agent_call_pools WHERE kind='sprint'") == 4
    with pytest.raises(ExecutionError, match="below_committed_budget"):
        await h.store.set_budget(h.owner, {"project_id": "p", "call_limit": 3, "expected_revision": 1, "request_key": "decrease"})


@pytest.mark.asyncio
async def test_cancel_releases_only_unspent_capacity_once(loop_h):
    h = loop_h
    await h.store.set_budget(h.owner, {"project_id": "p", "call_limit": 10, "expected_revision": None, "request_key": "pool"})
    rid = await h.start(calls=4, project_id="p")
    row = await h.store.claim("core")
    await ready_steps(h, row)
    reserved = await h.store.reserve_attempt(row, 0, MODEL["one"], "unsent")
    charged = await h.store.reserve_attempt(row, 1, MODEL["one"], "uncertain")
    await h.store.charge_attempt(row, charged)
    await h.store.cancel(h.owner, rid, "cancel")
    await h.store.stop(row, "owner_cancelled", unknown=True)
    await h.store.cancel(h.owner, rid, "cancel")
    run = (await h.store.get(h.owner, rid))["run"]
    assert run["status"] == "cancelled" and run["calls_used"] == run["unknown_calls"] == 1
    assert run["reserved_calls"] == 0
    assert await h.pg.fetchval("SELECT state FROM agent_attempts WHERE id=$1", reserved) == "released"
    pool = await h.pg.fetchrow("SELECT * FROM agent_call_pools")
    assert pool["consumed"] == 1 and pool["reserved"] == 0
    await invariants(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["fixed","follow_budget","none"])
async def test_growth_wall_modes_restart_and_fencing(loop_h, mode):
    h = loop_h
    rid = await h.start(budget={"start_calls": 10, "wall_mode": mode, "wall_seconds": None if mode == "none" else 3600})
    row = await h.store.claim("old-core")
    original = row["deadline"]
    await exhaust(h, row)
    assert await evaluate(h, row) == "grown"
    await h.store.release(row)
    new_store = Store(h.pg, "test-lane")
    recovered = await new_store.claim("new-core")
    if mode == "follow_budget":
        assert (recovered["deadline"] - recovered["created_at"]).total_seconds() == 50 * 360
    else:
        assert recovered["deadline"] == original
    with pytest.raises(ExecutionError, match="stale_controller"):
        await h.store.heartbeat(row)
    assert recovered["generation"] > row["generation"]
    assert recovered["id"] == rid


@pytest.mark.asyncio
async def test_invalid_plan_exhausts_basic_retries_then_requires_second_eye(loop_h, monkeypatch):
    monkeypatch.setattr("core.execution.failure_store.retry_delay", lambda *a, **k: .001)
    h = loop_h
    rid = await h.start(calls=50)
    row = await h.store.claim("core")
    calls = 0
    async def invalid(*args):
        nonlocal calls
        calls += 1
        return {"text": "not a valid plan"}
    await Executor(h.store, h.service, invalid, poll=.001).execute(row)
    run = (await h.store.get(h.owner,rid))["run"]
    assert run["status"] == "paused" and run["pause_reason"] == "second_eye_unavailable"
    assert run["calls_used"] == calls == 3 and not run["growth_events"]


@pytest.mark.asyncio
async def test_check_failure_recovery_does_not_replan_and_final_call_completes_without_growth(loop_h, monkeypatch):
    monkeypatch.setattr("core.execution.failure_store.retry_delay", lambda *a, **k: .001)
    h = loop_h
    rid = await h.start(calls=6)
    row = await h.store.claim("core")
    calls = 0
    async def bad(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {"text":canonical({"tasks":[{"id":str(calls),"instruction":"answer","dependencies":[],"criteria_ids":["final"],"output_kind":"answer"}]})}
        return {"text":canonical({"content":"Missing section","citations":["S1"]})}
    await Executor(h.store,h.service,bad,poll=.001).execute(row)
    failed = (await h.store.get(h.owner,rid))["run"]
    assert failed["pause_reason"] == "second_eye_unavailable" and failed["calls_used"] == 4 and not failed["growth_events"]
    rid = await h.start("finished",calls=2)
    row = await h.store.claim("core2")
    async def good(*args):
        if "Return ONLY JSON {\"tasks\"" in args[1]:
            return {"text":canonical({"tasks":[{"id":"final","instruction":"answer","dependencies":[],"criteria_ids":["final"],"output_kind":"answer"}]})}
        return {"text":canonical({"content":"Conclusion S1","citations":["S1"]})}
    await Executor(h.store,h.service,good,poll=.001).execute(row)
    finished = (await h.store.get(h.owner,rid))["run"]
    assert finished["status"] == "completed" and finished["calls_used"] == 2 and not finished["growth_events"]


@pytest.mark.asyncio
async def test_charge_outage_fails_closed_and_unsent_restart_refunds_once(loop_h, monkeypatch):
    h = loop_h
    rid = await h.start()
    row = await h.store.claim("core")
    fake = AsyncMock()
    monkeypatch.setattr(h.store,"charge_attempt",AsyncMock(side_effect=RuntimeError("DB unavailable")))
    await Executor(h.store,h.service,fake,poll=.001).execute(row)
    fake.assert_not_awaited()
    run = (await h.store.get(h.owner,rid))["run"]
    assert run["calls_used"] == run["reserved_calls"] == 0
    assert await h.pg.fetchval("SELECT count(*) FROM agent_attempts WHERE state='released'") == 1
    monkeypatch.undo()
    rid = await h.start("unsent-restart")
    row = await h.store.claim("old-core")
    await ready_steps(h,row,n=1)
    attempt = await h.store.reserve_attempt(row,0,MODEL["one"],"unsent")
    await h.store.release(row)
    recovered = await h.store.claim("new-core")
    assert recovered["id"] == rid
    assert await h.pg.fetchval("SELECT state FROM agent_attempts WHERE id=$1",attempt) == "released"
    with pytest.raises(ExecutionError,match="stale_controller"):
        await h.store.charge_attempt(row,attempt)
    await invariants(h)


@pytest.mark.asyncio
async def test_project_grant_can_admit_1000_calls_and_revocation_stops_dispatch(loop_h):
    h = loop_h
    team = await h.store.team(h.owner,TEAM["id"])
    request = {"task":"Supplied evidence S1","evidence_ids":["S1"],"acceptance":CONTRACT}
    await h.pg.execute("INSERT INTO agent_execution_grants(lane,owner,id,project_id,authority_boundary,task_digest,team_digest,models_digest,allow_long_runs,call_ceiling) VALUES ($1,$2,'project-grant','p',$3,$4,$5,$6,TRUE,1600)",
                       h.owner.lane,h.owner.owner,h.owner.boundary,task_digest(request),digest(team),digest(MODEL))
    rid = await h.start(calls=1000,project_id="p")
    row = await h.store.claim("core")
    assert (await h.store.get(h.owner,rid))["run"]["effective_call_limit"] == 1000
    await h.pg.execute("UPDATE agent_execution_grants SET revoked=TRUE")
    with pytest.raises(ExecutionError,match="grant_revoked"):
        await h.store.loop_next(row)
    assert (await h.store.get(h.owner,rid))["run"]["calls_used"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_after",[0,1,2])
async def test_generic_restart_reuses_plan_and_accepted_checkpoints(loop_h, crash_after):
    h = loop_h
    contract = {"criteria":CONTRACT["criteria"] + [{"id":"second","output_kind":"answer","checks":{}}]}
    rid = await h.start(acceptance=contract)
    row = await h.store.claim("old-core")
    calls = 0
    async def fake(binding,prompt):
        nonlocal calls
        calls += 1
        assert "S1" in prompt
        if calls == 1:
            return {"text":canonical({"tasks":[
                {"id":"first","instruction":"first answer","dependencies":[],"criteria_ids":["final"],"output_kind":"answer"},
                {"id":"second","instruction":"second answer","dependencies":["first"],"criteria_ids":["second"],"output_kind":"answer"}]})}
        return {"text":canonical({"content":"Conclusion S1","citations":["S1"]})}
    async def interrupt(row,ordinal):
        if ordinal == crash_after:
            raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await Executor(h.store,h.service,fake,poll=.001,after_checkpoint=interrupt).execute(row)
    store = Store(h.pg,"test-lane")
    service = Admission(store,h.service.catalog)
    recovered = await store.claim("new-core")
    if recovered:
        await Executor(store,service,fake,poll=.001).execute(recovered)
    run = (await store.get(h.owner,rid))["run"]
    assert run["status"] == "completed" and calls == run["calls_used"] == 3
    assert run["accepted_output_count"] == 2


@pytest.mark.asyncio
async def test_parallel_wave_reserves_both_before_transport_and_settles_before_completion(loop_h):
    h = loop_h
    rid = await h.start()
    row = await h.store.claim("core")
    workers = 0
    both = asyncio.Event()
    async def fake(binding,prompt):
        nonlocal workers
        if "Return ONLY JSON {\"tasks\"" in prompt:
            return {"text":canonical({"tasks":[
                {"id":"first","instruction":"answer","dependencies":[],"criteria_ids":["final"],"output_kind":"answer"},
                {"id":"second","instruction":"support","dependencies":[],"criteria_ids":[],"output_kind":"intermediate"}]})}
        workers += 1
        # Both requests were reserved, even if the other has not charged yet.
        assert await h.pg.fetchval("SELECT count(*) FROM agent_attempts WHERE run_id=$1",rid) == 3
        if workers == 2:
            both.set()
        await asyncio.wait_for(both.wait(),5)
        return {"text":canonical({"content":"Conclusion S1","citations":["S1"]})}
    await Executor(h.store,h.service,fake,poll=.001).execute(row)
    run = (await h.store.get(h.owner,rid))["run"]
    assert run["status"] == "completed" and workers == 2 and run["calls_used"] == 3
    assert not await h.pg.fetchval("SELECT count(*) FROM agent_attempts WHERE state IN ('reserved','dispatched')")
    await invariants(h)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_parallel_optional_recovery_keeps_controller_after_required_output(loop_h, monkeypatch, restart):
    """Settled attempts do not imply a settled wave during recovery backoff."""
    monkeypatch.setattr("core.execution.failure_store.retry_delay", lambda *a, **k: 0)
    h = loop_h
    rid = await h.start()
    row = await h.store.claim("old-core")
    failed, checkpointed = asyncio.Event(), asyncio.Event()
    original_record, original_wait = h.store.record_failure, h.store.failure_wait
    optional_calls = 0

    async def record(*args, **kwargs):
        episode = await original_record(*args, **kwargs)
        failed.set()
        return episode

    async def wait(*args, **kwargs):
        # Hold recovery between attempts until the required output commits.
        await checkpointed.wait()
        return await original_wait(*args, **kwargs)

    monkeypatch.setattr(h.store, "record_failure", record)
    monkeypatch.setattr(h.store, "failure_wait", wait)

    async def fake(binding, prompt):
        nonlocal optional_calls
        if 'Return ONLY JSON {"tasks"' in prompt:
            return {"text": canonical({"tasks": [
                {"id": "first", "instruction": "answer", "dependencies": [], "criteria_ids": ["final"], "output_kind": "answer"},
                {"id": "second", "instruction": "support", "dependencies": [], "criteria_ids": [], "output_kind": "intermediate"}]})}
        if '"id":"second"' in prompt:
            optional_calls += 1
            if optional_calls == 1:
                return {"text": "invalid JSON"}
        else:
            await asyncio.wait_for(failed.wait(), 5)
        return {"text": canonical({"content": "Conclusion S1", "citations": ["S1"]})}

    async def after_checkpoint(row, ordinal):
        if ordinal != 1:
            return
        run = (await h.store.get(h.owner, rid))["run"]
        assert run["accepted_output_count"] == 1 and run["status"] == "running"
        assert not await h.pg.fetchval("SELECT count(*) FROM agent_attempts WHERE run_id=$1 AND state IN ('reserved','dispatched','unknown')", rid)
        assert await h.pg.fetchval("SELECT state FROM agent_failure_episodes WHERE run_id=$1", rid) == "backoff"
        if restart:
            raise asyncio.CancelledError()
        checkpointed.set()

    executor = Executor(h.store, h.service, fake, poll=.001, after_checkpoint=after_checkpoint)
    if restart:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(executor.execute(row), 10)
        store = Store(h.pg, "test-lane")
        recovered = await store.claim("new-core")
        assert recovered["id"] == rid and recovered["generation"] > row["generation"]
        checkpointed.set()
        await asyncio.wait_for(Executor(store, h.service, fake, poll=.001).execute(recovered), 10)
    else:
        await asyncio.wait_for(executor.execute(row), 10)
    run = (await h.store.get(h.owner, rid))["run"]
    assert run["status"] == "completed" and run["calls_used"] == 4 and optional_calls == 2
    assert run["unknown_calls"] == run["reserved_calls"] == 0
    assert await h.pg.fetchval("SELECT state FROM agent_failure_episodes WHERE run_id=$1", rid) == "resolved"
    await invariants(h)


@pytest.mark.asyncio
async def test_partial_output_pauses_then_owner_resumes_checkpoint_50_to_100(loop_h):
    h = loop_h
    contract = {"criteria": CONTRACT["criteria"] + [{"id": "second", "output_kind": "answer", "checks": {}}]}
    rid = await h.start(calls=50, acceptance=contract)
    row = await h.store.claim("core")
    calls = 0
    async def fake(binding,prompt):
        nonlocal calls
        calls += 1
        if calls % 2:
            cid = "final" if calls == 1 else "second" if calls >= 51 else None
            return {"text": canonical({"tasks": [{"id": str(calls), "instruction": "work " + str(calls), "dependencies": [],
                "output_kind": "answer" if cid else "intermediate", "criteria_ids": [cid] if cid else []}]})}
        return {"text": canonical({"content": "Conclusion S1", "citations": ["S1"]})}
    await Executor(h.store, h.service, fake, poll=.001).execute(row)
    run = (await h.store.get(h.owner,rid))["run"]
    assert run["pause_reason"] == "budget_exhausted_with_output" and run["accepted_output_count"] == 1
    args = {"run_id": rid, "expected_revision": run["revision"], "request_key": "owner-resume", "budget": {"start_calls":100}}
    first = await h.store.resume(h.owner,args)
    assert await h.store.resume(h.owner,args) == first
    row = await h.store.claim("restarted-core")
    await Executor(h.store,h.service,fake,poll=.001).execute(row)
    final = (await h.store.get(h.owner,rid))["run"]
    assert final["status"] == "completed" and final["calls_used"] == calls == 52
    assert final["accepted_output_count"] == 2


@pytest.mark.asyncio
async def test_owner_resume_requires_grant_above_200_and_pool_growth_shortfall(loop_h):
    h = loop_h
    await h.store.set_budget(h.owner,{"project_id":"p","call_limit":30,"expected_revision":None,"request_key":"pool"})
    rid = await h.start(calls=10,project_id="p")
    row = await h.store.claim("core")
    await exhaust(h,row)
    assert await evaluate(h,row) == "pool_shortfall"
    run = (await h.store.get(h.owner,rid))["run"]
    assert run["effective_call_limit"] == 10
    with pytest.raises(ExecutionError, match="confirmation_required"):
        await h.store.resume(h.owner,{"run_id":rid,"expected_revision":run["revision"],"request_key":"too-large","budget":{"start_calls":400}})
    agent = Authority("test-lane","owner","agent",{},"agent")
    with pytest.raises(ExecutionError, match="not_found"):
        await h.store.resume(agent,{"run_id":rid,"expected_revision":run["revision"],"request_key":"agent"})
    await invariants(h)


@pytest.mark.asyncio
async def test_owner_commands_actual_protocol_and_sprint_links_fail_closed(harness, monkeypatch):
    h = harness
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "owner-a")
    monkeypatch.setenv("BOB_EXECUTION_BOUNDARY", "worker")
    from db import init_db
    from tests.test_owner_sessions import stepup
    await init_db()
    owner_headers, _ = await stepup(h.client, {"Authorization": "Bearer "+create_portal_token("owner-a", {}), "X-Bob-Broker-Link": "owner-protocol-link"}, "budgets.list", {})
    await h.save(definition=TEAM)
    response = await h.client.post("/agent-services/runs",headers=h.headers,json={
        "contract_version":2,"team":TEAM["id"],"team_revision":1,"request_key":"loop-start","task":"S1",
        "evidence_ids":["S1"],"acceptance":CONTRACT,"budget":{"start_calls":50,"wall_mode":"none","wall_seconds":None}})
    assert response.status == 202
    rid = (await response.json())["run"]["id"]
    row = await h.store.claim("core")
    await h.store.stop(row,"no_progress",unknown=False)
    run = (await h.store.get(h.actor,rid))["run"]
    request = {"contract_version":2,"expected_revision":run["revision"],"request_key":"owner-resume","budget":{"start_calls":100}}
    for token in (create_agent_token("owner-a",faces=["researcher"]), create_portal_token("owner-a",{})):
        denied = await h.client.post("/agent-services/runs/"+rid+"/resume",headers={"Authorization":"Bearer "+token},json=request)
        assert denied.status in {403,404}
    resumed = await h.client.post("/agent-services/runs/"+rid+"/resume",headers=owner_headers,json=request)
    assert resumed.status == 200 and (await resumed.json())["run"]["effective_call_limit"] == 100
    # Resource service is absent, even if a sprint pool table record exists.
    response, detail = await h.start("sprint-denied",sprint_id="s")
    assert response.status == 409 and detail["error"]["code"] == "resource_service_unavailable"


@pytest.mark.asyncio
async def test_project_budget_owner_resource_scope_revision_and_idempotency(harness, monkeypatch):
    import uuid
    from auth import create_access_token
    h = harness
    monkeypatch.setenv("BOB_DEFAULT_OWNER", "owner-a")
    monkeypatch.setenv("BOB_EXECUTION_BOUNDARY", "worker")
    from db import init_db
    from tests.test_owner_sessions import stepup
    await init_db()
    owner_headers, _ = await stepup(h.client, {"Authorization": "Bearer "+create_portal_token("owner-a", {}), "X-Bob-Broker-Link": "owner-budget-link"}, "budgets.list", {})
    project = uuid.uuid4()
    await h.pg.execute("INSERT INTO projects(id,user_id) VALUES ($1,'owner-a')",project)
    args = {"contract_version":2,"project_id":str(project),"call_limit":500,"expected_revision":None,"request_key":"pool"}
    foreign = {"Authorization":"Bearer "+create_access_token("foreign")}
    assert (await h.client.post("/agent-services/budgets",headers=foreign,json=args)).status in {403,404}
    first = await h.client.post("/agent-services/budgets",headers=owner_headers,json=args)
    assert first.status == 200
    result = await first.json()
    repeated = await h.client.post("/agent-services/budgets",headers=owner_headers,json=args)
    assert (await repeated.json())["operation_id"] == result["operation_id"]
    conflict = await h.client.post("/agent-services/budgets",headers=owner_headers,json={**args,"request_key":"wrong-revision"})
    assert conflict.status == 409
    assert await h.pg.fetchval("SELECT count(*) FROM agent_call_pools") == 1
