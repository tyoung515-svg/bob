"""Failure policy adversarial cases. No providers, DB, sockets or substitutions."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest

from core.execution.failure import classify, effect_state, fingerprint, parse_audit, retry_delay, select_auditor, validate_output
from core.execution.models import ExecutionError, definition, TOPOLOGY


BINDING = {"backend": "fixture", "model": "m1", "enabled": True, "exact_calls": True, "transport": "text"}


@pytest.mark.asyncio
@pytest.mark.parametrize("has_calls", [False, True])
@pytest.mark.parametrize("has_jobs", [False, True])
async def test_owner_flag_snapshots_independent_call_and_job_accounts(has_calls, has_jobs):
    from unittest.mock import AsyncMock
    from core.execution.store import Store

    row = {"lane": "lane", "owner": "owner", "id": "run", "phase": "dispatch",
           "initial_budget": {}, "request": {}, "selected_grant_id": "grant", "selected_grant_revision": 1}
    calls = {"call_limit": 100, "consumed": 3, "unknown_calls": 1} if has_calls else None
    jobs = {"lane": "lane", "owner": "owner", "run_id": "run", "held": 1,
            "seconds_held": 1800, "unknown": 0, "seconds_used": 12} if has_jobs else None
    conn = AsyncMock()
    conn.fetchrow.side_effect = [calls, jobs]
    conn.fetchval.side_effect = [2 if has_calls else None, "flag"]
    store = Store(None, "lane")
    store._event = AsyncMock()

    await store._flag(conn, row, "grant_revoked")

    insert = conn.fetchval.call_args_list[-1].args
    context = json.loads(insert[-1])
    assert context["account"] == calls  # absence is not a zero/authorized call budget
    assert context["reserved_calls"] == (2 if has_calls else None)
    assert context["job_account"] == ({k: v for k, v in jobs.items() if k not in {"lane", "owner", "run_id"}} if has_jobs else None)
    assert context["selected_grant_id"] == "grant" and context["selected_grant_revision"] == 1
    store._event.assert_awaited_once()
    # Flag creation cannot settle holds, create call authority, or acquire queue /
    # account locks after its caller has already locked the owning run.
    assert len(conn.execute.await_args_list) == 1
    assert conn.execute.call_args.args[0].startswith("UPDATE agent_runs SET owner_flag=")
    assert all("FOR UPDATE" not in call.args[0] and "advisory" not in call.args[0]
               for call in conn.fetchrow.call_args_list + conn.fetchval.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["initial", "basic", "audit", "guided"])
@pytest.mark.parametrize("boundary", ["reserve", "charge"])
async def test_current_policy_blocks_every_recovery_dispatch_boundary(monkeypatch, purpose, boundary):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from core.execution.store import Store

    monkeypatch.setenv("BOB_SETTINGS_ID", "fixture-settings")
    conn = AsyncMock()
    conn.fetchrow.return_value = {"policy": {"posture": "confirm", "scope": {}}, "revision": 2}
    @asynccontextmanager
    async def transaction():
        yield conn
    store = Store(None, "lane")
    store.transaction = transaction
    store._lock_pools = AsyncMock()
    store._fenced = AsyncMock(return_value={"lane": "lane", "owner": "owner", "id": "run"})
    row = {"lane": "lane", "owner": "owner", "id": "run"}
    with pytest.raises(ExecutionError, match="policy_narrowed"):
        if boundary == "reserve":
            await store.reserve_attempt(row, 0, BINDING, "source", episode_id=None if purpose == "initial" else "episode", purpose=purpose)
        else:
            await store.charge_attempt(row, "attempt")
    conn.execute.assert_not_awaited()  # no reservation, charge, or retry counter mutation


@pytest.mark.parametrize("code,status,expected", [
    ("provider_error", 429, "transient"), ("provider_error", 503, "transient"),
    ("provider_error", 401, "forbidden"), ("provider_error", 403, "forbidden"),
    ("provider_error", 400, "forbidden"), ("provider_error", 404, "forbidden"),
    ("provider_error", 408, "transient"), ("provider_error", 302, "forbidden"),
    ("check_failed", None, "correction"), ("test_failed", None, "correction"),
    ("invalid_plan_schema", None, "correction"), ("invalid_provider_response", None, "correction"),
    ("provider_timeout", None, "transient"), ("call_budget_exhausted", None, "forbidden"),
    ("authorization_revoked", None, "forbidden"), ("wall_time_expired", None, "forbidden"),
    ("credential_epoch_changed", None, "forbidden"), ("opaque_call_accounting", None, "forbidden"),
])
def test_failure_classes(code, status, expected):
    exc = ExecutionError(code, 502, {"status": status} if status else None)
    assert classify(exc) == (expected, code)


@pytest.mark.parametrize("header,expected", [(None, 2), ("9999999", 60), ("-1", 2), ("NaN", 2),
                                           ("inf", 2), ("invalid", 2), ("5", 5)])
def test_bounded_retry_after(header, expected):
    assert retry_delay(0, header) == expected
    assert 8 <= retry_delay(1, header) <= 60


def test_retry_after_date_and_jitter_are_bounded():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert retry_delay(0, "Thu, 01 Jan 2026 00:00:20 GMT", now=now) == 20
    assert retry_delay(0, jitter=99) == 3
    assert retry_delay(1, jitter=-99) == 8


def test_different_alias_is_same_model_and_order_intersects_approval_and_health():
    models = {"alias": BINDING, "unhealthy": {**BINDING, "model": "m2", "healthy": False},
              "blocked": {**BINDING, "model": "m3", "enabled": False},
              "opaque": {**BINDING, "model": "m4", "exact_calls": False},
              "allowed": {**BINDING, "model": "m5"}, "unapproved": {**BINDING, "model": "m6"}}
    team = {"audit_bindings": ["alias", "unhealthy", "blocked", "opaque", "allowed"]}
    assert select_auditor(team, models, models, BINDING) == ("allowed", models["allowed"])
    changed = deepcopy(models)
    changed["allowed"]["model"] = "new-alias"
    with pytest.raises(ExecutionError, match="second_eye_unavailable"):
        select_auditor(team, models, changed, BINDING)
    for current in (None, {}, {"unapproved": models["unapproved"]}):
        with pytest.raises(ExecutionError, match="second_eye_unavailable"):
            select_auditor(team, models, current, BINDING)


def audit():
    return {"schema_version": 1, "failed_step_id": "7", "attempt_ids": ["a1"],
            "reviewer": {"binding_id": "forged", "model_id": "forged"}, "cause": "invalid output",
            "evidence_refs": ["check:1"], "uncertainties": [], "effect_assessment": "none",
            "recommended_changes": ["correct the JSON"], "required_checks": ["acceptance"],
            "recommendation": "guided_retry", "confidence": "medium"}


def parse(value):
    return parse_audit(json.dumps(value), step_id="7", attempt_ids=["a1", "a2"], evidence_refs=["check:1"],
                       check_ids=["acceptance"], binding_id="auditor", binding=BINDING)


def test_reviewer_is_server_stamped_and_evidence_is_validated():
    result = parse(audit())
    assert result["reviewer"] == {"binding_id": "auditor", "model_id": "m1"}


@pytest.mark.asyncio
async def test_audit_pause_does_not_prepare_again_after_releasing_controller():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core.execution.failure import FailureRecovery

    data = audit()
    data["recommendation"] = "pause"
    episode = {"id": "episode", "state": "audit", "auditor": "auditor",
               "audit_input": {"attempt_ids": ["a1"], "evidence_refs": ["check:1"], "check_ids": ["acceptance"]}}
    store = SimpleNamespace(charge_attempt=AsyncMock(), audit_result=AsyncMock(return_value={"state": "paused"}),
                            failure_for_step=AsyncMock(side_effect=ExecutionError("stale_controller", 409)),
                            record_failure=AsyncMock())
    executor = SimpleNamespace(store=store, admission=SimpleNamespace(revalidate=AsyncMock()),
                               _call=AsyncMock(return_value={"text": json.dumps(data)}))
    recovery = FailureRecovery(executor)
    recovery.audit_binding = AsyncMock(return_value=("auditor", BINDING))
    assert await recovery.call({}, {"ordinal": 7}, BINDING, "source",
                               prepared=("attempt", episode, "audit", BINDING, "audit prompt")) is None
    store.audit_result.assert_awaited_once()
    store.failure_for_step.assert_not_awaited()
    store.record_failure.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("schema_version", True), ("failed_step_id", "8"),
    ("attempt_ids", ["foreign"]), ("attempt_ids", []), ("attempt_ids", ["a1", "a1"]),
    ("evidence_refs", ["made-up"]), ("evidence_refs", []), ("required_checks", ["invented"]),
    ("required_checks", []), ("reviewer", {}), ("reviewer", {"binding_id": "x", "model_id": "x", "tools": True}),
    ("cause", ""), ("uncertainties", "no"), ("recommendation", "audit_auditor"),
    ("effect_assessment", "safe_to_replay"), ("confidence", 1), ("new_authority", "allow")])
def test_malformed_audit_never_authorizes_a_retry(field, value):
    data = audit(); data[field] = value
    with pytest.raises(ExecutionError, match="invalid_audit"):
        parse(data)


def test_audit_size_duplicate_keys_and_missing_fields():
    for raw in ('{"schema_version":1,"schema_version":1}', "x"*16001, "[]", "null"):
        with pytest.raises(ExecutionError, match="invalid_audit"):
            parse_audit(raw, step_id="7", attempt_ids=[], evidence_refs=[], check_ids=[], binding_id="audit", binding=BINDING)
    data = audit(); del data["uncertainties"]
    with pytest.raises(ExecutionError, match="invalid_audit"):
        parse(data)


def test_fingerprint_survives_replan_id_wording_and_cycle_changes():
    spec = {"phase": "task", "criteria_ids": ["c2", "c1"], "id": "t1", "cycle": 1, "instruction": "old"}
    step = {"ordinal": 7, "spec": spec}
    replanned = {"ordinal": 100, "spec": {**spec, "id": "new", "instruction": "different", "cycle": 99, "criteria_ids": ["c1", "c2"]}}
    assert fingerprint(step, "check_failed") == fingerprint(replanned, "check_failed")
    assert fingerprint(step, "check_failed") != fingerprint({"ordinal": 7, "spec": {**spec, "base_digest": "new"}}, "check_failed")


@pytest.mark.parametrize("value", [["x", "x"], [True], "x", [""]])
def test_audit_order_is_explicit_validated_team_data(value):
    with pytest.raises(ExecutionError, match="invalid_audit_bindings"):
        definition({"id": "team", "schema_version": 1, "audit_bindings": value})


def test_four_step_invalid_manager_json_is_a_failure_before_publication():
    row = {"request": {"task": "task"}, "snapshot": {"team": {"topology": TOPOLOGY}}}
    with pytest.raises(ExecutionError, match="invalid_manager_plan"):
        validate_output(row, {"ordinal": 0}, "broken JSON")
    assert validate_output(row, {"ordinal": 0}, '{"tasks":["bounded instruction"]}')["passed"]


@pytest.mark.parametrize("effect_class", ["idempotent_by_key", "workspace_mutation", "external_or_unknown"])
def test_non_read_only_effects_need_server_reconciliation_evidence(effect_class):
    step = {"effect_class": effect_class, "spec": {"operation_key": "key", "base_digest": "base"}}
    assert effect_state(step) == "unknown"
    step["effect_receipt"] = {"status": "not_applied"}
    assert effect_state(step) == "unknown"
    step["effect_receipt"] = {"status": "not_applied", "verified": True, "receipt_ref": "receipt:1",
                              "request_key": "key", "base_digest": "base", "output_digest": "unchanged"}
    assert effect_state(step) == "not_applied"
    step["effect_receipt"]["status"] = "applied"
    assert effect_state(step) == "applied"


@pytest.mark.parametrize("effect_class,field", [("idempotent_by_key", "request_key"), ("workspace_mutation", "base_digest")])
def test_request_key_or_workspace_base_cannot_change_on_retry(effect_class, field):
    step = {"effect_class": effect_class, "spec": {"operation_key": "original", "base_digest": "original"},
            "effect_receipt": {"status": "not_applied", "verified": True, "receipt_ref": "receipt:1",
                               "request_key": "original", "base_digest": "original", "output_digest": "same"}}
    step["effect_receipt"][field] = "changed"
    assert effect_state(step) == "unknown"


@pytest.mark.asyncio
async def test_live_audit_selection_probes_only_approved_different_model_candidates():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core.execution.failure import FailureRecovery
    models = {"alias": BINDING, "a": {**BINDING, "model": "m2"}, "b": {**BINDING, "model": "m3"},
              "unapproved": {**BINDING, "model": "m4"}}
    health = AsyncMock(side_effect=[False, True])
    executor = SimpleNamespace(store=SimpleNamespace(heartbeat=AsyncMock(return_value="running")),
                               admission=SimpleNamespace(models=AsyncMock(return_value=models)), audit_health=health)
    row = {"snapshot": {"team": {"audit_bindings": ["alias", "a", "b"]}, "models": models}}
    assert await FailureRecovery(executor).audit_binding(row, BINDING, {}) == ("b", models["b"])
    assert [a.args[0]["model"] for a in health.await_args_list] == ["m2", "m3"]


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [False, TimeoutError(), RuntimeError()])
async def test_dispatcher_health_is_non_generating_and_fails_closed(monkeypatch, result):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from core.execution.dispatcher import Dispatcher, EXACT_BACKENDS
    from core import health_probe, lane_config
    health = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
    factory = Mock(return_value=SimpleNamespace(health_check=health))
    monkeypatch.setattr(health_probe, "_make_client", factory)
    monkeypatch.setattr(lane_config, "configured_mode", lambda: False)
    binding = {**BINDING, "backend": sorted(EXACT_BACKENDS)[0]}
    assert not await Dispatcher().healthy(binding)
    health.assert_awaited_once()
    assert not await Dispatcher().healthy({**binding, "backend": "unapproved-transport"})
    factory.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("code,status,expected", [("provider_error", 429, "transient"), ("provider_error", 503, "transient"),
    ("provider_error", 401, "forbidden"), ("provider_timeout", None, "transient"), ("invalid_provider_response", None, "correction")])
async def test_native_transport_preserves_typed_failure_without_retrying_inside_adapter(monkeypatch, code, status, expected):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from core.backends import openai_codex
    from core.execution.dispatcher import Dispatcher
    from core import lane_config
    call = AsyncMock(side_effect=openai_codex.CodexProviderError("sanitized", status, code=code, retry_after="99"))
    monkeypatch.setattr(openai_codex, "OpenAICodexClient", lambda: SimpleNamespace(complete=call))
    monkeypatch.setattr(lane_config, "configured_mode", lambda: False)
    with pytest.raises(ExecutionError) as error:
        await Dispatcher()({**BINDING, "backend": "openai_codex"}, "original source")
    assert classify(error.value)[0] == expected
    assert error.value.detail == {"status": status, "retry_after": "99"}
    call.assert_awaited_once()
    assert call.await_args.kwargs["retry_unauthorized"] is False
