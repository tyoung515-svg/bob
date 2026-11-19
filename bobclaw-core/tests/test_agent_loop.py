"""Offline validation, accounting guard and executor seams. No metered calls.

Durable SQL and race assertions live in gateway/tests/test_agent_loop_postgres.py;
the lightweight executor fixture here is deliberately not a production store.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.execution.budgets import grown_deadline, next_limit
from core.execution.admission import Admission
from core.execution.call_guard import charged_call, consume_permit, consume_transport, required
from core.execution.dispatcher import Dispatcher
from core.execution.executor import Executor
from core.execution.models import ExecutionError, LOOP_TOPOLOGY, definition, eligibility, start_request
from core.permissions import execution_vouch, verify_execution_vouch
from core.execution.planning import acceptance_contract, check_output, parse_plan
from core.execution.recovery import context_for

CONTRACT = {"criteria": [{"id": "answer", "required": True, "output_kind": "answer",
                          "checks": {"required_sections": ["Conclusion"], "require_citations": True,
                                     "min_length": 10, "max_length": 1000}}]}
MODEL = {"one": {"backend": "deepseek_v4_flash", "model": "fixture", "enabled": True,
                 "transport": "text", "exact_calls": True}}
TEAM = {"schema_version": 1, "id": "loop", "topology": LOOP_TOPOLOGY,
        "roles": {r: "one" for r in ("manager", "worker", "critic")}}


def task(key="t1", dependencies=None, **extra):
    return {"id": key, "instruction": "Use supplied evidence", "dependencies": dependencies or [],
            "output_kind": "answer", "criteria_ids": ["answer"], **extra}


def plan(tasks):
    return json.dumps({"tasks": tasks})


@pytest.mark.parametrize("tasks,code", [
    ([task("a", ["b"]), task("b", ["a"])], "plan_cycle"),
    ([task(str(n), [str(n-1)] if n else []) for n in range(5)], "plan_too_deep"),
    ([task(str(n)) for n in range(17)], "plan_too_wide"),
    ([task(output_kind="shell")], "unknown_output_kind"),
    ([task(output_kind=[])], "unknown_output_kind"),
    ([task(checks={"json_type":[]})], "invalid_acceptance"),
    ([task(dependencies=["missing"])], "unknown_dependency"),
    ([task(), task()], "duplicate_task"),
    ([task(criteria_ids=["invented"])], "invalid_criterion_assignment"),
    ([task(output_kind="intermediate")], "invalid_criterion_assignment"),
    ([task(tools=["exec"])], "unsupported_fields"),
    ([task("source"+str(n)) for n in range(16)] + [task("child"+str(n),["source0"]) for n in range(16)], "plan_too_wide"),
])
def test_untrusted_plans_cannot_create_authority(tasks, code):
    with pytest.raises(ExecutionError, match=code):
        parse_plan(plan(tasks), CONTRACT)


def test_valid_dependencies_and_one_model_lane():
    tasks = [task("research", output_kind="intermediate", criteria_ids=[]), task("answer", ["research"])]
    assert parse_plan(plan(tasks), acceptance_contract(CONTRACT))["tasks"] == tasks
    assert eligibility(definition(TEAM), MODEL)["review_independence"] == "same_model"


@pytest.mark.parametrize("raw,fail", [
    ('{"content":"", "citations":[]}', "schema_valid"),
    ('{"content":"Conclusion unknown", "citations":["foreign"]}', "supplied_evidence_only"),
    ('{"content":"Conclusion [source:foreign]", "citations":["S1"]}', "supplied_evidence_only"),
    ('{"content":"Missing required section", "citations":["S1"]}', "answer:required_sections"),
    ('{"content":"Conclusion enough text", "citations":[]}', "answer:citations_required"),
    ('{"content":"Conclusion ' + "x" * 1000 + '", "citations":["S1"]}', "answer:length_bounds"),
    ('{"content":"Conclusion enough text", "citations":["S1"], "done":true}', "schema_valid"),
])
def test_checks_are_deterministic_and_use_only_supplied_ids(raw, fail):
    check = check_output(raw, task(), CONTRACT, ["S1"])
    assert not check["passed"] and fail in check["failures"] and not check["criteria_ids"]


def test_accepted_answer_and_json_schema_checks():
    assert check_output(json.dumps({"content": "Conclusion [source:S1]", "citations": ["S1"]}), task(), CONTRACT, ["S1"])["passed"]
    contract = {"criteria": [{"id": "data", "output_kind": "json", "checks": {"json_type": "array"}}]}
    work = task(output_kind="json", criteria_ids=["data"])
    check = lambda content: check_output(json.dumps({"content": content, "citations": []}), work, contract, [])
    assert check('[1,2]')["passed"]
    assert "json_type" in check('{}')["failures"]
    assert "json_valid" in check('broken')["failures"]
    assert "json_valid" in check('[NaN]')["failures"]


def test_planner_checks_can_add_constraints_but_cannot_weaken_acceptance():
    work = task(checks={"min_length":1,"require_citations":False})
    parse_plan(plan([work]),CONTRACT)
    receipt = check_output(json.dumps({"content":"Conclusion enough text","citations":[]}),work,CONTRACT,["S1"])
    assert not receipt["passed"] and "answer:citations_required" in receipt["failures"]
    with pytest.raises(ExecutionError,match="unsupported_fields"):
        parse_plan(plan([task(checks={"run_command":"write-file"})]),CONTRACT)


@pytest.mark.asyncio
async def test_direct_backend_inner_call_is_denied_before_credentials_or_http():
    from core.backends.claude import ClaudeClient
    from core.backends.openai_codex import OpenAICodexClient
    token = required.set(True)
    try:
        # No constructors/credentials: methods must reject at their entry boundary.
        with pytest.raises(ExecutionError,match="unreserved_model_call"):
            await OpenAICodexClient.complete(object(),[],model="fixture")
        with pytest.raises(ExecutionError,match="unreserved_model_call"):
            await ClaudeClient.chat(object(),[],model="fixture")
    finally:
        required.reset(token)


@pytest.mark.parametrize("old,new", [(1,10),(10,50),(30,50),(50,100),(100,200),(200,400),(400,800),(800,1600)])
def test_next_growth_is_a_total(old, new):
    assert next_limit(old) == new


@pytest.mark.parametrize("mode", ["fixed", "follow_budget", "none"])
def test_absolute_wall_rule_across_rebuilt_context(mode):
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    row = {"created_at": start, "deadline": None if mode == "none" else start + timedelta(hours=1),
           "wall_rule_seconds": 360, "request": {"budget": {"wall_mode": mode}}}
    before = copy.deepcopy(row)
    after_restart = grown_deadline(row, 50)
    assert after_restart == (start + timedelta(hours=5) if mode == "follow_budget" else row["deadline"])
    assert row == before


def test_recovery_context_excludes_transcript_and_keeps_source_lineage():
    row = {"request": {"task": "Original supplied evidence", "evidence_ids": ["S1"], "acceptance": CONTRACT},
           "transcript": ["unbounded history"]}
    outputs = [{"id": "o1", "digest": "digest", "criteria_ids": ["answer"], "content": "accepted text"}]
    rebuilt = context_for(row, outputs, list(range(100)))
    value = json.loads(rebuilt)
    assert value["recent_receipts"] == list(range(92,100))
    assert "unbounded history" not in rebuilt and "S1" in rebuilt and "accepted text" in rebuilt


@pytest.mark.asyncio
async def test_unreserved_and_hidden_inner_calls_are_denied_before_transport():
    token = required.set(True)
    try:
        with pytest.raises(ExecutionError, match="unreserved_model_call"):
            await Dispatcher()(MODEL["one"], "unreserved")
        with charged_call("charged-attempt"):
            consume_permit()
            consume_transport()
            # A second inference, including an SDK retry, cannot share a permit.
            with pytest.raises(ExecutionError, match="unreserved_model_call"):
                consume_transport()
            with pytest.raises(ExecutionError, match="unreserved_model_call"):
                await Dispatcher()(MODEL["one"], "hidden inner call")
        with pytest.raises(ExecutionError, match="unreserved_model_call"):
            consume_transport()
    finally:
        required.reset(token)


@pytest.mark.asyncio
async def test_fake_1200_call_executor_has_no_fixed_step_or_call_truncation():
    count = 0
    persisted = []
    row = {"id": "run", "deadline": None, "snapshot": {"team": TEAM, "models": MODEL},
           "request": {"task": "SOURCE", "evidence_ids": ["S1"], "acceptance": CONTRACT}}
    store = SimpleNamespace(heartbeat=AsyncMock(return_value="running"), stop=AsyncMock(), release=AsyncMock(),
                            loop_dependencies=AsyncMock(return_value=[]), failure_for_step=AsyncMock(return_value=None))

    async def next_work(row):
        if count == 1200:
            return {"action": "completed"}
        return {"action": "steps", "steps": [{"ordinal": count, "role": "manager" if count % 2 == 0 else "worker",
                 "spec": {"phase": "plan" if count % 2 == 0 else "task", "cycle": count // 2,
                          "dependencies": [], "instruction": "iteration " + str(count),
                          "criteria_ids": ["answer"], "output_kind": "answer"}}]}

    async def reserve(row, ordinal, binding, prompt, **recovery):
        assert ordinal == count
        return "attempt-" + str(ordinal)

    async def charge(row, attempt):
        persisted.append(attempt)

    async def fake(binding, prompt):
        nonlocal count
        consume_permit()
        consume_transport()
        assert persisted[-1] == "attempt-" + str(count)
        count += 1
        return {"text": plan([task()]) if count % 2 else json.dumps({"content": "Conclusion [source:S1]", "citations": ["S1"]}), "usage": {}}

    store.loop_next, store.reserve_attempt, store.charge_attempt = next_work, reserve, charge
    store.loop_view = AsyncMock(return_value={"row": row, "outputs": [], "recent_receipts": []})
    store.checkpoint_loop = AsyncMock(return_value=True)
    await Executor(store, SimpleNamespace(revalidate=AsyncMock()), fake, poll=.001).execute(row)
    assert count == len(persisted) == store.checkpoint_loop.await_count == 1200
    store.stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None, "backoff", "audit", "guided"])
async def test_wave_reserves_all_members_before_transport_including_recovery(state):
    row = {"id": "run", "deadline": None, "snapshot": {"team": TEAM, "models": MODEL},
           "request": {"task": "SOURCE", "evidence_ids": ["S1"], "acceptance": CONTRACT}}
    auditor = {**MODEL["one"], "model": "independent-fixture"}
    row["snapshot"] = {"team": {**TEAM, "audit_bindings": ["audit"]},
                       "models": {**MODEL, "audit": auditor}}
    steps = [{"ordinal": n, "role": "worker", "spec": {"phase": "task", **task(str(n))}}
             for n in (1, 2)]
    episodes = {n: {"id": str(n), "state": state, "last_failure": {"code": "check_failed"},
                    "audit": {"recommended_changes": ["correct the answer"]},
                    "audit_input": {"failed_step_id": str(n), "attempt_ids": ["failed-" + str(n)],
                                    "evidence_refs": ["S1"], "check_ids": ["acceptance"]}}
                for n in (1, 2)} if state else {}
    reservations, charged, settled = {}, set(), set()

    async def reserve(row, ordinal, binding, prompt, **policy):
        # Yield before the second durable reservation, as a real DB call does.
        # A per-worker reservation would let the first transport run here.
        if ordinal == 2 and not reservations:
            raise AssertionError("first member was not reserved")
        await asyncio.sleep(.01)
        attempt = str(ordinal) + "-" + policy["purpose"]
        assert attempt not in reservations
        reservations[attempt] = (ordinal, policy["purpose"])
        return attempt

    async def charge(row, attempt):
        assert attempt in reservations and attempt not in charged
        charged.add(attempt)

    async def set_auditor(row, episode_id, key, binding):
        episode = episodes[int(episode_id)]
        episode["auditor"] = key
        return episode

    async def audit_result(row, episode_id, attempt, output, parsed, usage):
        episode = episodes[int(episode_id)]
        episode.update(state="guided", audit=parsed)
        return episode

    async def dispatch(binding, prompt):
        consume_permit()
        consume_transport()
        first_purpose = {None: "initial", "backoff": "basic", "audit": "audit", "guided": "guided"}[state]
        assert {"1-" + first_purpose, "2-" + first_purpose} <= reservations.keys()
        if binding == auditor:
            data = json.loads(prompt.split("\n", 1)[1])
            return {"text": json.dumps({"schema_version": 1, "failed_step_id": data["failed_step_id"],
                "attempt_ids": data["attempt_ids"], "reviewer": {"binding_id": "audit", "model_id": auditor["model"]},
                "cause": "invalid answer", "evidence_refs": ["S1"], "uncertainties": [],
                "effect_assessment": "none", "recommended_changes": ["correct the answer"],
                "required_checks": ["acceptance"], "recommendation": "guided_retry", "confidence": "high"})}
        return {"text": json.dumps({"content": "Conclusion [source:S1]", "citations": ["S1"]})}

    async def checkpoint(row, step, attempt, output, usage):
        assert attempt in charged
        settled.add(step["ordinal"])
        return "completed" if len(settled) == 2 else "checkpointed"

    store = SimpleNamespace(
        loop_next=AsyncMock(return_value={"action": "steps", "steps": steps}),
        loop_view=AsyncMock(return_value={"row": row, "outputs": [], "recent_receipts": []}),
        loop_dependencies=AsyncMock(return_value=[]),
        failure_for_step=AsyncMock(side_effect=lambda row, step: episodes.get(step["ordinal"])),
        failure_wait=AsyncMock(return_value=0), set_auditor=set_auditor, audit_result=audit_result,
        reserve_attempt=reserve, charge_attempt=charge, checkpoint_loop=checkpoint,
        heartbeat=AsyncMock(return_value="running"), stop=AsyncMock(),
        record_failure=AsyncMock(side_effect=AssertionError("unexpected recovery failure")))
    admission = SimpleNamespace(revalidate=AsyncMock(), models=AsyncMock(return_value=row["snapshot"]["models"]))
    await Executor(store, admission, dispatch, poll=.001).execute(row)
    assert settled == {1, 2}
    assert len(charged) == len(reservations) == (4 if state == "audit" else 2)
    store.stop.assert_not_awaited()
    store.record_failure.assert_not_awaited()


@pytest.mark.asyncio
async def test_db_reservation_outage_prevents_dispatch():
    store = SimpleNamespace(loop_next=AsyncMock(side_effect=RuntimeError("DB unavailable")), stop=AsyncMock())
    send = AsyncMock()
    row = {"id": "run", "snapshot": {"team": TEAM}}
    await Executor(store, SimpleNamespace(revalidate=AsyncMock()), send).execute(row)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_loop_cannot_spawn_a_public_run_with_a_fresh_budget():
    token = required.set(True)
    try:
        with pytest.raises(ExecutionError,match="nested_run_start_forbidden"):
            await Admission(None,None).start(None,{})
    finally:
        required.reset(token)


@pytest.mark.asyncio
async def test_injected_monotonic_attempt_timeout_survives_none_wall():
    ticks = iter([0, 181])
    async def blocked(*args):
        await asyncio.Event().wait()
    executor = Executor(SimpleNamespace(heartbeat=AsyncMock(return_value="running")), None, blocked,
                        monotonic=lambda: next(ticks), poll=.001)
    with pytest.raises(ExecutionError, match="provider_timeout"):
        await executor._call({"deadline": None}, MODEL["one"], "source")


@pytest.mark.parametrize("op,field", [("runs.resume","budget"),("runs.resume","expected_revision"),("budgets.set","project_id"),("budgets.set","call_limit")])
def test_owner_operation_vouch_binds_new_fields(op, field):
    import time
    now = int(time.time())
    envelope = {"operation":op,"args":{field:100},"authority":{},"iat":now,"exp":now+30}
    signed = execution_vouch(envelope,"fixture-secret")
    assert verify_execution_vouch(envelope,signed,"fixture-secret")
    envelope["args"][field] = 200
    assert not verify_execution_vouch(envelope,signed,"fixture-secret")
