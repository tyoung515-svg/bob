"""Offline contracts and real HTTP exact-transport tests; never metered calls."""
import copy
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from core.execution.admission import Admission
from core.execution.api import router, SERVICE_KEY, lane_id, policy_epoch
from core.execution.dispatcher import Dispatcher, exact_models
from core.execution.executor import Executor, prompt_for
from core.execution.models import (Authority, ExecutionError, TOPOLOGY, budget, definition,
                                   eligibility, start_request)
from core.execution.store import Store
from core.permissions import execution_vouch, verify_execution_vouch, scope_vouch


MODEL = {"one": {"backend": "deepseek_v4_flash", "model": "fixture-model", "enabled": True,
                 "transport": "text", "exact_calls": True}}
TEAM = {"schema_version": 1, "id": "one-team", "topology": TOPOLOGY,
        "roles": {r: "one" for r in ("manager", "worker", "critic")}}
REQUEST = {"contract_version": 2, "team": "one-team", "team_revision": 1, "task": "source:original evidence",
           "request_key": "start-1", "budget": {"start_calls": 50, "wall_mode": "none", "wall_seconds": None}}


@pytest.mark.parametrize("spec,kind", [
    (None, "model_task"),  # SQL NULL on saved fixed-topology runs, including recovery.
    ("null", "model_task"),
    ({}, "model_task"),
    ('{"phase":"task"}', "model_task"),  # Original text-loop steps have no kind.
    ({"kind": "model_task"}, "model_task"),
    *[({"kind": kind}, kind) for kind in (
        "workspace_read", "workspace_edit", "worker_command", "registered_check", "proposal_publish")],
])
async def test_public_receipts_preserve_legacy_and_typed_step_identity(spec, kind):
    from core.execution.models import LOOP_TOPOLOGY

    legacy = spec is None or spec == "null"
    roles = ["manager", "worker", "critic", "manager"] if legacy else ["worker"]
    steps = [{"ordinal": i, "role": role, "state": "completed", "spec": spec,
              "output": f"output-{i}", "output_digest": f"digest-{i}", "usage": "{}", "checks": None}
             for i, role in enumerate(roles)]
    # Non-model receipts must not even look up a model binding.
    team = {**TEAM, "topology": TEAM["topology"] if legacy else LOOP_TOPOLOGY,
            "roles": TEAM["roles"] if kind == "model_task" else {}}
    row = dict(lane="lane", owner="owner", id="run", actor="owner", team=team["id"], team_revision=1,
               snapshot={"team": team, "models": MODEL if kind == "model_task" else {},
                         "review_independence": "same_model"},
               request={**REQUEST, "acceptance": {"criteria": []}}, status="completed", reason=None,
               revision=1, selected_grant_id=None, selected_grant_revision=None, cleanup_pending=False,
               phase="integrate", event_seq=0, initial_budget=REQUEST["budget"], owner_flag=False,
               wall_rule_seconds=360, created_at=None, updated_at=None, deadline=None)
    conn = SimpleNamespace(
        fetchrow=AsyncMock(side_effect=[{"call_limit": 50, "consumed": len(steps), "unknown_calls": 0},
                                       None, {"reserved": 0}]),
        fetch=AsyncMock(side_effect=[steps, [], [], [], [], [], []]),
        fetchval=AsyncMock(side_effect=[len(steps), False]))

    view = await Store(None, "lane")._public(conn, row)

    assert view["run"]["status"] == "completed"
    assert len(view["receipts"]) == len(steps)
    for receipt in view["receipts"]:
        assert receipt["kind"] == kind
        assert receipt["model_face"] == ("one" if kind == "model_task" else None)
        assert receipt["model_pin"] == ("fixture-model" if kind == "model_task" else None)
    if legacy:
        assert view["run"]["result"] == "output-3"


@pytest.mark.parametrize("value", [True, False, 1.5, "10", None, -1, 0, 3, 2**63])
def test_budget_strict_integers(value):
    with pytest.raises(ExecutionError):
        budget({"start_calls": value, "wall_mode": "none", "wall_seconds": None})


@pytest.mark.parametrize("calls", [10, 50, 100, 200, 17])
@pytest.mark.parametrize("mode,seconds", [("fixed", 3600), ("follow_budget", 72000), ("none", None)])
def test_budget_preserves_requested_values(calls, mode, seconds):
    value = {"start_calls": calls, "wall_mode": mode, "wall_seconds": seconds}
    assert budget(value) == value


def test_confirmation_is_blocked_without_a_substitute_grant():
    for calls in (201, 1000, 2**63 - 1):
        with pytest.raises(ExecutionError) as exc:
            budget({"start_calls": calls, "wall_mode": "none", "wall_seconds": None})
        assert exc.value.code == "confirmation_required"
        assert exc.value.detail == {"blocked_reasons": ["owner_grant_service_unavailable"]}
    for field in ("budget_grant_id", "auto_escalate", "owner", "scope", "allow_long_runs"):
        with pytest.raises(ExecutionError):
            start_request({**REQUEST, field: "self-approved"})


@pytest.mark.parametrize("mode,seconds", [("none", 60), ("fixed", None), ("fixed", True),
                                          ("fixed", 1.2), ("fixed", 2**63), ("unlimited", None)])
def test_wall_conflicts_and_representation_errors(mode, seconds):
    with pytest.raises(ExecutionError):
        budget({"start_calls": 10, "wall_mode": mode, "wall_seconds": seconds})


def test_one_model_and_missing_catalog_are_honest():
    team = definition(TEAM)
    assert eligibility(team, MODEL) == {"eligibility": "runnable", "runnable": True,
                                        "blocked_reasons": [], "review_independence": "same_model"}
    assert "catalog_unavailable" in eligibility(team, None)["blocked_reasons"]
    assert not eligibility(team, {})["runnable"]
    assert "independent_review_required" in eligibility({**team, "review_independence": "different_model"}, MODEL)["blocked_reasons"]
    assert "team_is_draft" in eligibility({**team, "state": "draft"}, MODEL)["blocked_reasons"]
    cli = exact_models({"one": {**MODEL["one"], "backend": "claude_code"}})
    assert "opaque_call_accounting:worker" in eligibility(team, cli)["blocked_reasons"]


def test_reviewer_independence_requires_worker_critic_difference():
    models = {**MODEL, "two": {**MODEL["one"], "model": "second-model"}}
    team = {**definition(TEAM), "review_independence": "different_model", "roles": {**TEAM["roles"], "manager": "two"}}
    assert not eligibility(team, models)["runnable"]


def envelope():
    now = int(time.time())
    return {"operation": "runs.start", "args": copy.deepcopy(REQUEST),
            "authority": Authority("lane", "owner", "actor", {"may_touch": ["allowed/**"]}, "agent").as_dict(),
            "iat": now, "exp": now + 30}


@pytest.mark.parametrize("target", ["project_id", "team", "team_revision", "task", "budget", "request_key"])
def test_vouch_binds_every_resource_field(target):
    value = envelope()
    signed = execution_vouch(value, "test-secret")
    assert verify_execution_vouch(value, signed, "test-secret")
    value["args"][target] = "forged-resource"
    assert not verify_execution_vouch(value, signed, "test-secret")


def test_vouch_rejects_scope_vouch_and_actor_lane_operation_changes():
    value = envelope()
    assert not verify_execution_vouch(value, scope_vouch({}, "test-secret"), "test-secret")
    for field in ("owner", "actor", "lane", "scope"):
        changed = copy.deepcopy(value)
        changed["authority"][field] = "forged"
        assert not verify_execution_vouch(changed, execution_vouch(value, "test-secret"), "test-secret")
    value["exp"] = value["iat"] - 1
    assert not verify_execution_vouch(value, execution_vouch(value, "test-secret"), "test-secret")
    assert not verify_execution_vouch(envelope(), "", "")


def test_ownership_is_owner_and_initiating_boundary():
    owner = Authority("lane", "owner", "owner", {}, "portal")
    agent = Authority("lane", "owner", "agent-a", {"may_touch": ["a/**"]}, "agent")
    row = {"lane": "lane", "owner": "owner", "boundary": agent.boundary}
    assert owner.permits(row) and agent.permits(row)
    assert not Authority("foreign", "owner", "owner", {}, "human").permits(row)
    assert not Authority("lane", "foreign", "foreign", {}, "human").permits(row)
    assert not Authority("lane", "owner", "agent-a", {}, "agent").permits(row)


@pytest.mark.asyncio
async def test_storage_failure_never_falls_back():
    store = Store(None, "test-lane")
    assert not await store.available()
    service = Admission(store, AsyncMock(return_value=MODEL))
    with pytest.raises(ExecutionError, match="execution_storage_unavailable"):
        await service.start(Authority("test-lane", "owner", "owner", {}, "human"), REQUEST)


@pytest.mark.asyncio
async def test_real_exact_transport_once_on_success_or_failure(monkeypatch):
    from core.config import config
    from core import lane_config
    calls = []
    status = [200]

    async def model(request):
        payload = await request.json()
        calls.append(payload)
        assert payload["model"] == "fixture-model"
        assert "tools" not in payload
        return web.json_response({"choices": [{"message": {"content": "evidence answer"}}], "usage": {"total_tokens": 5}}, status=status[0])

    app = web.Application()
    app.router.add_post("/chat/completions", model)
    server = TestServer(app)
    await server.start_server()
    monkeypatch.setattr(config, "DEEPSEEK_BASE_URL", str(server.make_url("/")).rstrip("/"))
    monkeypatch.setattr(config, "DEEPSEEK_API_KEY", "fixture-unused-key")
    monkeypatch.setattr(lane_config, "configured_mode", lambda: False)
    try:
        send = Dispatcher()
        assert (await send(MODEL["one"], "supplied source"))["text"] == "evidence answer"
        for code in (401, 429, 500):
            status[0] = code
            with pytest.raises(ExecutionError):
                await send(MODEL["one"], "source")
        assert len(calls) == 4  # failed transports never retry or switch providers
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_core_forged_commands_and_missing_storage_fail_closed(monkeypatch):
    from core.config import config
    monkeypatch.setattr(config, "BOBCLAW_SECRET", "fixture-vouch-secret")
    app = web.Application()
    app.add_routes(router)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        value = envelope()
        assert (await client.post("/api/execution/runs.start", json={"envelope": value, "vouch": "forged"})).status == 403
        value["authority"]["lane"] = lane_id()
        value["authority"]["policy_epoch"] = policy_epoch(config.BOBCLAW_SECRET)
        response = await client.post("/api/execution/runs.start", json={"envelope": value, "vouch": execution_vouch(value, config.BOBCLAW_SECRET)})
        assert response.status == 503
        assert (await response.json())["error"]["code"] == "execution_storage_unavailable"
    finally:
        await client.close()


def test_joat_adapter_reads_only_explicit_unambiguous_lane_bindings(tmp_path, monkeypatch):
    from core import teams
    path = tmp_path / "imported.yaml"
    raw = "roles:\n  apex: {backend: deepseek_v4_flash}\n  worker: {backend: deepseek_v4_flash, escalation_chain: [glm_5_3]}\n  critic: {backend: deepseek_v4_flash}\n"
    path.write_text(raw, encoding="utf-8")
    monkeypatch.setattr(teams, "_custom_teams_dir", tmp_path)
    drafts = teams.execution_drafts(MODEL)
    assert [row["id"] for row in drafts] == ["imported"]
    assert drafts[0]["roles"] == TEAM["roles"]
    assert drafts[0]["state"] == "draft"
    assert drafts[0]["origin"]["proposed_alternatives"]["worker"] == ["glm_5_3"]
    assert path.read_text(encoding="utf-8") == raw
    ambiguous = teams.execution_drafts({**MODEL, "another": MODEL["one"]})
    assert ambiguous[0]["roles"]["manager"] is None


def test_context_rebuilt_from_preserved_sources_and_checkpoints():
    row = {"request": {**REQUEST, "evidence_ids": ["source:123"], "acceptance": "cite the source"}}
    steps = [{"output": '{"tasks":["one bounded task"]}'}, {"output": "worker source:123"}, {"output": "critic caveat"}]
    for ordinal in range(4):
        prompt = prompt_for(row, steps, ordinal)
        assert "source:123" in prompt and REQUEST["task"] in prompt
    assert "worker source:123" in prompt_for(row, steps, 3)
    assert "critic caveat" in prompt_for(row, steps, 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("completed", [0, 1, 2, 3])
async def test_executor_charges_before_transport_and_skips_persisted_steps(completed):
    """Executor seam only; real storage/restarts are tested against Postgres."""
    order = []
    steps = [{"ordinal": n, "role": role, "state": "completed" if n < completed else "ready",
              "output": '{"tasks":["bounded source task"]}' if n == 0 else "saved output"}
             for n, role in enumerate(("manager", "worker", "critic", "manager"))]
    store = SimpleNamespace(steps=AsyncMock(return_value=steps), heartbeat=AsyncMock(return_value="running"),
                            stop=AsyncMock(), release=AsyncMock(), failure_for_step=AsyncMock(return_value=None))

    async def reserve(row, ordinal, binding, prompt, **recovery):
        return "attempt-" + str(ordinal)

    async def charge(row, attempt):
        ordinal = int(attempt.rsplit("-", 1)[1])
        order.append(("charged", ordinal))
        return "attempt-" + str(ordinal)

    async def send(binding, prompt):
        assert order[-1][0] == "charged"
        ordinal = order[-1][1]
        order.append(("transport", ordinal))
        return {"text": '{"tasks":["bounded source task"]}' if ordinal == 0 else "answer", "usage": {}}

    async def checkpoint(row, ordinal, attempt, output, usage):
        assert order[-1] == ("transport", ordinal)
        order.append(("persisted", ordinal))
        return True

    store.reserve_attempt, store.charge_attempt, store.checkpoint = reserve, charge, checkpoint
    admission = SimpleNamespace(revalidate=AsyncMock())
    row = {"id": "run", "request": REQUEST, "deadline": None,
           "snapshot": {"team": TEAM, "models": MODEL}}
    await Executor(store, admission, send, poll=.001).execute(row)
    assert order == [(kind, n) for n in range(completed, 4) for kind in ("charged", "transport", "persisted")]
    store.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_admission_disables_start_if_pump_could_not_initialize():
    service = Admission(None, AsyncMock(return_value=MODEL))
    service.enabled = False
    with pytest.raises(ExecutionError, match="execution_executor_unavailable"):
        await service.start(Authority("lane", "owner", "owner", {}, "human"), REQUEST)


@pytest.mark.asyncio
async def test_known_provider_refusal_is_charged_failed_not_unknown():
    store = SimpleNamespace(steps=AsyncMock(return_value=[{"ordinal": 0, "role": "manager", "state": "ready"}]),
                            reserve_attempt=AsyncMock(return_value="attempt"), charge_attempt=AsyncMock(),
                            failure_for_step=AsyncMock(return_value=None), record_failure=AsyncMock(return_value={"state": "paused"}), stop=AsyncMock(),
                            heartbeat=AsyncMock(return_value="running"))
    send = AsyncMock(side_effect=ExecutionError("provider_error", 502))
    row = {"id": "run", "request": REQUEST, "deadline": None, "snapshot": {"team": TEAM, "models": MODEL}}
    await Executor(store, SimpleNamespace(revalidate=AsyncMock()), send, poll=.001).execute(row)
    store.charge_attempt.assert_awaited_once()
    send.assert_awaited_once()
    assert store.record_failure.await_args.kwargs["returned"] is False
    store.stop.assert_not_awaited()  # durable failure policy owns settlement


def test_actual_runtime_snapshot_contains_identical_broker_and_migration(tmp_path):
    root = Path(__file__).resolve().parents[2]
    target = tmp_path / "runtime-snapshot"
    subprocess.run([sys.executable, str(root / "deploy/runtime/snapshot.py"), str(root), str(target)], check=True)
    for relative in ("portal-broker/portal-broker.py", "migrations/2026-11-03_agent_runs.sql",
                     "bobclaw-core/core/execution/executor.py", "bobclaw-gateway/routers/execution.py"):
        assert (target / relative).read_bytes() == (root / relative).read_bytes()
    assert not (target / "bobclaw-core/tests/test_durable_execution.py").exists()


@pytest.mark.asyncio
async def test_injected_deadline_closes_transport_without_wall_clock_wait():
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    async def wait_forever(*args):
        await asyncio.Event().wait()
    transport = AsyncMock(side_effect=wait_forever)
    store = SimpleNamespace(heartbeat=AsyncMock(return_value="running"))
    executor = Executor(store, None, transport, clock=lambda: now, poll=.001)
    with pytest.raises(ExecutionError, match="wall_time_expired"):
        await executor._call({"deadline": now - timedelta(seconds=1)}, MODEL["one"], "source")
