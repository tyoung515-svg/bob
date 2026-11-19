"""4.4 acceptance with REAL disposable Postgres, fake model transports only.

Uses the existing local temporary-schema fixture. Unset DSN is a skip, never
evidence that persistence, transaction or crash recovery tests have passed.
"""
import asyncio
from dataclasses import replace
import json
import time
from unittest.mock import AsyncMock

import pytest

from tests.test_durable_runs_v2 import pg, harness, MODEL, TEAM
from core.execution.admission import Admission
from core.execution.executor import Executor
from core.execution.models import ExecutionError, canonical, LOOP_TOPOLOGY
from core.execution.store import Store


AUDITOR = {**MODEL["one"], "model": "fixture-different-identity"}
MODELS = {**MODEL, "alias": MODEL["one"], "eye": AUDITOR}


@pytest.fixture
async def recovery_h(harness, monkeypatch):
    h = harness
    h.catalog.return_value = MODELS
    monkeypatch.setattr("core.execution.failure_store.retry_delay", lambda *a, **k: .001)
    return h


@pytest.fixture
async def installed_recovery_h(recovery_h, monkeypatch):
    """Policy-enabled lanes also enforce 3.3's installed credential generation."""
    from auth import create_access_token, create_portal_token
    from config import config
    from core import credential_state
    import credential_auth
    import owner_sessions

    h = recovery_h
    h.active_credentials = {'epoch': 1}
    h.owner_clock = [int(time.time() / 30) * 30 + 5]
    monkeypatch.setenv('BOB_SETTINGS_ID', 'a' * 12)
    monkeypatch.setenv('BOB_CREDENTIAL_EPOCH', '1')
    monkeypatch.setenv('BOB_DEFAULT_OWNER', h.actor.owner)
    monkeypatch.setenv('BOB_EXECUTION_BOUNDARY', 'worker')
    # Only the root-published pointer is synthetic. Keep generation, JWT,
    # owner-session, stored-authority and reserve/charge/resume checks active.
    monkeypatch.setattr(credential_state, 'active', lambda: dict(h.active_credentials))
    monkeypatch.setattr(owner_sessions, 'clock', lambda: h.owner_clock[0])
    monkeypatch.setattr(config, 'BOBCLAW_PASSWORD', 'fixture-recovery-password')
    monkeypatch.setattr(config, 'TOTP_SECRET', 'JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP')
    await owner_sessions.initialize()
    await credential_auth.initialize()
    # The shared harness issued its human JWT before installed mode was enabled.
    # Mutate the shared headers so its save/start closures use the fresh token.
    h.headers['Authorization'] = 'Bearer ' + create_access_token(h.actor.owner)
    h.actor = replace(h.actor, credential_epoch=1)
    h.portal_headers = {'Authorization': 'Bearer ' + create_portal_token(h.actor.owner, {}),
                        'X-Bob-Broker-Link': 'fixture-recovery-broker-link'}
    return h


async def admitted(h, *, audits=True, calls=100, team=None):
    response, _ = await h.save(definition=team or {**TEAM, "audit_bindings": ["alias", "eye"] if audits else ["alias"]})
    assert response.status == 200, await response.text()
    response, started = await h.start(budget={"start_calls": calls, "wall_mode": "none", "wall_seconds": None})
    assert response.status == 202
    return started["run"]["id"], await h.store.claim("controller")


def advice(prompt, **changes):
    data = json.loads(prompt.split("\n", 1)[1])
    return canonical({"schema_version": 1, "failed_step_id": data["failed_step_id"], "attempt_ids": data["attempt_ids"],
        "reviewer": {"binding_id": "spoof", "model_id": "spoof"}, "cause": "fixture failure",
        "evidence_refs": data["evidence_refs"], "uncertainties": [], "effect_assessment": "none",
        "recommended_changes": ["preserve the required output schema"], "required_checks": ["acceptance"],
        "recommendation": "guided_retry", "confidence": "high", **changes})


@pytest.mark.parametrize('stage', ['backoff', 'audit', 'guided'])
@pytest.mark.parametrize('dispatched', [False, True])
@pytest.mark.parametrize('epoch_changed', [False, True])
async def test_policy_narrowing_fences_recovery_and_only_explicit_safe_resume_continues(installed_recovery_h, monkeypatch, stage, dispatched, epoch_changed):
    from core.execution.failure import FailureRecovery
    from tests.test_settings_postgres import change
    from tests.test_owner_sessions import stepup
    import credential_auth
    h = installed_recovery_h
    await h.store.set_policy(h.actor, change(actions=['model_call']))
    rid, row = await admitted(h)
    assert row['run_authority']['credential_epoch'] == 1
    original_failure, original_audit = h.store.record_failure, h.store.audit_result
    async def stop_at_stage(original, *args, **kwargs):
        episode = await original(*args, **kwargs)
        if episode['state'] == stage:
            raise asyncio.CancelledError()
        return episode
    async def failure(*args, **kwargs):
        return await stop_at_stage(original_failure, *args, **kwargs)
    async def audit(*args, **kwargs):
        return await stop_at_stage(original_audit, *args, **kwargs)
    monkeypatch.setattr(h.store, 'record_failure', failure)
    monkeypatch.setattr(h.store, 'audit_result', audit)
    send, calls = transport(h)
    with pytest.raises(asyncio.CancelledError):
        await Executor(h.store, h.service, send, poll=.001).execute(row)
    monkeypatch.setattr(h.store, 'record_failure', original_failure)
    monkeypatch.setattr(h.store, 'audit_result', original_audit)
    row = await h.store.claim('pending-recovery')
    step = (await h.store.steps(row))[0]
    recovery = FailureRecovery(Executor(h.store, h.service, send, poll=.001))
    prepared = await recovery.prepare(row, step, MODEL['one'], 'source')
    attempt = prepared[0]
    if dispatched:
        await h.store.charge_attempt(row, attempt)
    before = await h.pg.fetchrow('SELECT * FROM agent_failure_episodes')
    sent = len(calls)
    await h.store.set_policy(h.actor, change('narrow', 1))
    with pytest.raises(ExecutionError):
        await h.store.charge_attempt(row, attempt)
    paused = await h.pg.fetchrow('SELECT * FROM agent_failure_episodes')
    assert paused['state'] == 'paused'
    assert paused['resume_state'] == (None if dispatched else stage)
    assert paused['retry_at'] == before['retry_at']
    for field in ('basic_retries', 'audit_calls', 'guided_calls'):
        assert paused[field] == before[field]
    assert await h.pg.fetchval('SELECT count(*) FROM agent_flags') == 1
    await h.store.set_policy(h.actor, change('widen', 2, ['model_call']))
    assert await h.store.claim('no-auto-resume') is None
    run = (await h.store.get(h.actor, rid))['run']
    assert run['status'] == 'paused' and run['reason'] == 'policy_narrowed'
    args = {'run_id': rid, 'expected_revision': run['revision'], 'request_key': 'explicit-resume'}
    async def resume(headers):
        return await h.client.post('/agent-services/runs/' + rid + '/resume', headers=headers,
            json={'contract_version': 2, **{k: v for k, v in args.items() if k != 'run_id'}})

    owner_headers, _ = await stepup(h.client, h.portal_headers, resource={'run_id': rid})
    if epoch_changed:
        # Simulate the password commit, first with a gateway still on the old
        # generation, then with a reloaded gateway and a fresh owner session.
        h.active_credentials['epoch'] = 2
        response = await resume(owner_headers)
        assert response.status == 503
        assert (await response.json())['error']['code'] == 'credential_generation_mismatch'
        monkeypatch.setenv('BOB_CREDENTIAL_EPOCH', '2')
        await credential_auth.initialize()
        response = await resume(owner_headers)
        assert response.status == 403
        assert (await response.json())['error']['code'] == 'owner_session_invalid'
        h.owner_clock[0] += 30  # The previous factor proof cannot be reused.
        owner_headers, _ = await stepup(h.client, h.portal_headers, resource={'run_id': rid})

    response = await resume(owner_headers)
    if dispatched:
        assert run['unknown_calls'] == 1
        assert response.status == 409
        assert (await response.json())['error']['code'] == 'run_not_resumable'
    elif epoch_changed:
        assert run['unknown_calls'] == 0
        assert response.status == 403
        assert (await response.json())['error']['code'] == 'credential_epoch_changed'
    else:
        assert run['unknown_calls'] == 0
        assert response.status == 200, await response.text()
        row = await h.store.claim('explicitly-resumed')
        assert row['run_authority']['credential_epoch'] == 1
        await Executor(h.store, h.service, send, poll=.001).execute(row)
        assert (await h.store.get(h.actor, rid))['run']['status'] == 'completed'
        counts = await h.pg.fetchrow('SELECT basic_retries,audit_calls,guided_calls FROM agent_failure_episodes')
        assert tuple(counts) == (2, 1, 1)
    if dispatched or epoch_changed:
        assert len(calls) == sent
        assert await h.store.claim('denied-resume') is None
        assert (await h.store.get(h.actor, rid))['run'] == run
        still_paused = await h.pg.fetchrow('SELECT * FROM agent_failure_episodes')
        for field in ('state', 'resume_state', 'retry_at', 'basic_retries', 'audit_calls', 'guided_calls'):
            assert still_paused[field] == paused[field]
    else:
        assert len(calls) > sent


def transport(h, error="transient", *, fail_guided=False, malformed_audit=False, crash=None):
    calls = []
    primary = 0
    async def send(binding, prompt):
        nonlocal primary
        calls.append((binding, prompt))
        if binding == AUDITOR:
            if crash == "audit":
                raise asyncio.CancelledError()
            return {"text": "bad" if malformed_audit else advice(prompt), "usage": {"total_tokens": 5}}
        primary += 1
        if primary <= 3 or fail_guided and primary == 4:
            if error == "invalid_json":
                return {"text": "bad JSON"}
            raise ExecutionError("provider_error", 502, {"status": 429 if error == "rate_limit" else 503})
        if crash == "guided" and "ONE GUIDED RETRY" in prompt:
            raise asyncio.CancelledError()
        return await h.fake(binding, prompt)
    return send, calls


@pytest.mark.parametrize("error", ["rate_limit", "transient", "invalid_json"])
async def test_two_basic_then_different_model_audit_one_guided_and_independent_checks(recovery_h, error):
    h = recovery_h
    rid, row = await admitted(h)
    send, calls = transport(h, error)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["status"] == "completed" and run["calls_used"] == 8
    assert [b["model"] for b, _ in calls[:5]] == [MODEL["one"]["model"]]*3 + [AUDITOR["model"], MODEL["one"]["model"]]
    episode = await h.pg.fetchrow("SELECT * FROM agent_failure_episodes")
    assert (episode["basic_retries"], episode["audit_calls"], episode["guided_calls"], episode["state"]) == (2, 1, 1, "resolved")
    parsed = json.loads(episode["audit"])
    assert parsed["reviewer"] == {"binding_id": "eye", "model_id": AUDITOR["model"]}
    event = await h.pg.fetchval("SELECT detail FROM agent_run_events WHERE kind='failure_recovered'")
    assert json.loads(event)["verification"] == "server_deterministic_checks"
    assert await h.pg.fetchval("SELECT count(*) FROM agent_flags") == 0


@pytest.mark.parametrize("malformed,fail_guided,reason,expected_calls", [(True, False, "invalid_audit", 4), (False, True, "guided_retry_failed", 5)])
async def test_auditor_schema_or_guided_failure_flags_once_without_recursion(recovery_h, malformed, fail_guided, reason, expected_calls):
    h = recovery_h
    rid, row = await admitted(h)
    send, calls = transport(h, malformed_audit=malformed, fail_guided=fail_guided)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["status"] == "paused" and run["reason"] == reason and run["calls_used"] == expected_calls
    assert len([b for b, _ in calls if b == AUDITOR]) == 1
    first = await h.store.flags(h.actor, {"run_id": rid})
    restarted = Store(h.pg, h.actor.lane)
    assert await restarted.claim("reconnect") is None
    assert await restarted.flags(h.actor, {"run_id": rid}) == first
    assert len(first["items"]) == 1 and run["flags"][0]["id"] == first["items"][0]["id"]
    detail = await restarted.flag_get(h.actor, {"flag_id": first["items"][0]["id"]})
    assert len(detail["flag"]["attempts"]) == expected_calls
    assert detail["flag"]["budget"]["calls_used"] == expected_calls
    assert detail["flag"]["stopped_context"]["account"]["consumed"] == expected_calls
    assert detail["flag"]["budget"]["job_account"] == run["job_account"]
    assert detail["flag"]["episode"]["raw_audit"] is not None or malformed


@pytest.mark.parametrize("audits", [False, True])
async def test_one_model_or_unhealthy_audit_pauses_without_substitution(recovery_h, audits):
    h = recovery_h
    rid, row = await admitted(h, audits=audits)
    if audits:
        h.catalog.return_value = {**MODELS, "eye": {**AUDITOR, "enabled": False}}
    send, calls = transport(h)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["reason"] == "second_eye_unavailable" and run["calls_used"] == 3 and len(calls) == 3
    assert len(run["flags"]) == 1


async def test_audit_transport_has_two_transient_retries_but_no_audit_of_auditor(recovery_h):
    h = recovery_h
    rid, row = await admitted(h)
    send = AsyncMock(side_effect=ExecutionError("provider_error", 502, {"status": 503}))
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["calls_used"] == 6 and run["reason"] == "second_eye_unavailable"
    assert await h.pg.fetchval("SELECT count(*) FROM agent_failure_episodes") == 1
    assert await h.pg.fetchval("SELECT audit_calls FROM agent_failure_episodes") == 3


async def test_exhausted_recovery_reservation_never_auto_grows(recovery_h):
    h = recovery_h
    rid, row = await admitted(h, calls=4)
    send, calls = transport(h)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["status"] == "paused" and run["calls_used"] == 4 and len(calls) == 4
    assert run["reason"] == "call_budget_exhausted" and run["effective_call_limit"] == 4
    assert not run["growth_events"] and len(run["flags"]) == 1


async def test_owner_budget_resume_continues_unused_guided_attempt_without_resetting_counts(recovery_h):
    h = recovery_h
    rid, row = await admitted(h, calls=4)
    send, calls = transport(h)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    episode_id = await h.pg.fetchval("SELECT id FROM agent_failure_episodes")
    assert await h.pg.fetchval("SELECT resume_state FROM agent_failure_episodes") == "guided"
    await h.store.resume(h.actor, {"run_id": rid, "expected_revision": run["revision"], "request_key": "owner-budget",
                                  "budget": {"start_calls": 100}})
    row = await h.store.claim("resumed")
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    assert (await h.store.get(h.actor, rid))["run"]["status"] == "completed"
    assert await h.pg.fetchval("SELECT id FROM agent_failure_episodes") == episode_id
    assert (await h.pg.fetchrow("SELECT basic_retries,audit_calls,guided_calls FROM agent_failure_episodes"))[:] == (2, 1, 1)
    assert await h.pg.fetchval("SELECT count(*) FROM agent_flags") == 1


async def test_restart_at_exhausted_backoff_does_not_enter_generic_auto_growth(recovery_h, monkeypatch):
    h = recovery_h
    await h.save(definition={**TEAM, "topology": LOOP_TOPOLOGY})
    contract = {"criteria": [{"id": "final", "output_kind": "answer", "checks": {}}]}
    _, started = await h.start(acceptance=contract, budget={"start_calls": 1, "wall_mode": "none", "wall_seconds": None})
    rid = started["run"]["id"]; row = await h.store.claim("controller")
    original = h.store.record_failure
    async def crash(*a, **kw):
        await original(*a, **kw)
        raise asyncio.CancelledError()
    monkeypatch.setattr(h.store, "record_failure", crash)
    with pytest.raises(asyncio.CancelledError):
        await Executor(h.store, h.service, AsyncMock(return_value={"text": "invalid JSON"}), poll=.001).execute(row)
    h.store = Store(h.pg, h.actor.lane)
    row = await h.store.claim("restarted")
    await Executor(h.store, Admission(h.store, h.catalog), AsyncMock(), poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["reason"] == "call_budget_exhausted" and run["calls_used"] == run["effective_call_limit"] == 1
    assert not run["growth_events"] and await h.pg.fetchval("SELECT basic_retries FROM agent_failure_episodes") == 0


async def test_crash_during_backoff_retains_counts_and_absolute_retry_at(recovery_h, monkeypatch):
    h = recovery_h
    rid, row = await admitted(h)
    original = h.store.record_failure
    async def crash(*a, **kw):
        result = await original(*a, **kw)
        if result["basic_retries"] == 1:
            raise asyncio.CancelledError()
        return result
    monkeypatch.setattr(h.store, "record_failure", crash)
    send, calls = transport(h)
    with pytest.raises(asyncio.CancelledError):
        await Executor(h.store, h.service, send, poll=.001).execute(row)
    before = await h.pg.fetchrow("SELECT * FROM agent_failure_episodes")
    h.store = Store(h.pg, h.actor.lane)
    h.service = Admission(h.store, h.catalog)
    row = await h.store.claim("rebooted")
    restored = await h.store.failure_for_step(row, (await h.store.steps(row))[0])
    assert restored["retry_at"] == before["retry_at"] and restored["basic_retries"] == 1
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    assert (await h.store.get(h.actor, rid))["run"]["status"] == "completed"
    assert await h.pg.fetchval("SELECT basic_retries FROM agent_failure_episodes") == 2


@pytest.mark.parametrize("stage", ["audit", "guided"])
async def test_crash_during_dispatched_recovery_charges_unknown_and_never_replays(recovery_h, stage):
    h = recovery_h
    rid, row = await admitted(h)
    send, calls = transport(h, crash=stage)
    with pytest.raises(asyncio.CancelledError):
        await Executor(h.store, h.service, send, poll=.001).execute(row)
    old_count = len(calls)
    restarted = Store(h.pg, h.actor.lane)
    assert await restarted.claim("rebooted") is None
    run = (await restarted.get(h.actor, rid))["run"]
    assert run["status"] == "paused" and run["reason"] == "unknown_provider_result"
    assert run["calls_used"] == old_count and run["unknown_calls"] == 1
    assert len(run["flags"]) == 1 and run["flags"][0]["episode_id"]
    assert await restarted.claim("again") is None


async def test_crash_after_audit_commit_allows_only_the_one_not_yet_dispatched_guided_call(recovery_h, monkeypatch):
    h = recovery_h
    rid, row = await admitted(h)
    original = h.store.audit_result
    async def crash(*a, **kw):
        await original(*a, **kw)
        raise asyncio.CancelledError()
    monkeypatch.setattr(h.store, "audit_result", crash)
    send, calls = transport(h)
    with pytest.raises(asyncio.CancelledError):
        await Executor(h.store, h.service, send, poll=.001).execute(row)
    h.store = Store(h.pg, h.actor.lane)
    row = await h.store.claim("rebooted")
    await Executor(h.store, Admission(h.store, h.catalog), send, poll=.001).execute(row)
    assert (await h.store.get(h.actor, rid))["run"]["status"] == "completed"
    assert await h.pg.fetchval("SELECT guided_calls FROM agent_failure_episodes") == 1
    assert len([b for b, _ in calls if b == AUDITOR]) == 1


@pytest.mark.parametrize("effect_class,effect", [("external_or_unknown", "unknown"), ("external_or_unknown", "applied"),
                                                ("workspace_mutation", "unknown"), ("idempotent_by_key", "applied")])
async def test_applied_or_uncertain_effects_never_replayed(recovery_h, effect_class, effect):
    h = recovery_h
    rid, row = await admitted(h)
    await h.pg.execute("UPDATE agent_run_steps SET effect_class=$1,effect_receipt=$2::jsonb WHERE run_id=$3 AND ordinal=0", effect_class, canonical({"status": effect}), rid)
    send, calls = transport(h)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["reason"] == "effect_" + effect and len(calls) == 1
    flag = await h.store.flag_get(h.actor, {"flag_id": run["flags"][0]["id"]})
    assert flag["flag"]["effects"] == effect


async def test_worker_unknown_transition_is_flagged_atomically_without_queue_changes(recovery_h):
    h = recovery_h
    rid, row = await admitted(h)
    for _ in range(2):
        await h.pg.execute("UPDATE agent_runs SET status='paused',reason='worker_outcome_unknown' WHERE id=$1", rid)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["owner_flag"] and len(run["flags"]) == 1
    assert await h.pg.fetchval("SELECT count(*) FROM agent_run_events WHERE kind='flag_raised'") == 1
    assert await h.store.claim("reconnect") is None


async def test_existing_worker_unknown_is_backfilled_once_on_migration(recovery_h):
    h = recovery_h
    rid, row = await admitted(h)
    await h.pg.execute("DROP TRIGGER agent_worker_unknown_flag ON agent_runs")
    await h.pg.execute("UPDATE agent_runs SET status='paused',reason='worker_outcome_unknown' WHERE id=$1", rid)
    assert await h.pg.fetchval("SELECT count(*) FROM agent_flags") == 0
    await h.store.initialize()
    await h.store.initialize()
    assert await h.pg.fetchval("SELECT count(*) FROM agent_flags") == 1
    assert await h.pg.fetchval("SELECT count(*) FROM agent_run_events WHERE kind='flag_raised'") == 1


@pytest.mark.parametrize("reason", ["cancel", "revoke", "deadline"])
async def test_control_change_during_backoff_prevents_every_further_dispatch(recovery_h, reason, monkeypatch):
    h = recovery_h
    rid, row = await admitted(h)
    original = h.store.record_failure
    async def change(*a, **kw):
        episode = await original(*a, **kw)
        if reason == "cancel":
            await h.store.cancel(h.actor, rid, "cancel-race")
        elif reason == "revoke":
            await h.pg.execute("UPDATE agent_runs SET authorization_revoked=TRUE WHERE id=$1", rid)
        else:
            await h.pg.execute("UPDATE agent_runs SET deadline=clock_timestamp()-interval '1 second' WHERE id=$1", rid)
        return episode
    monkeypatch.setattr(h.store, "record_failure", change)
    send, calls = transport(h)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert len(calls) == run["calls_used"] == 1
    assert run["status"] == ("cancelled" if reason == "cancel" else "paused")


async def test_flags_scope_idempotent_resolution_and_no_implicit_resume(recovery_h):
    h = recovery_h
    rid, row = await admitted(h, audits=False)
    send, calls = transport(h)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    flag = (await h.store.flags(h.actor, {}))["items"][0]
    for auth in (replace(h.actor, lane="foreign"), replace(h.actor, owner="foreign"),
                 replace(h.actor, actor="other-agent", token_type="agent")):
        with pytest.raises(ExecutionError, match="flag_not_found"):
            await h.store.flag_get(auth, {"flag_id": flag["id"]})
    args = {"flag_id": flag["id"], "expected_revision": flag["revision"], "request_key": "resolution-key",
            "decision": "inspected", "guidance": "Inspected; preserve the paused run."}
    result = await h.store.flag_resolve(h.actor, args)
    assert result["resolved"] and not result["run_resumed"]
    assert await h.store.flag_resolve(h.actor, args) == result
    with pytest.raises(ExecutionError, match="idempotency_conflict"):
        await h.store.flag_resolve(h.actor, {**args, "guidance": "different"})
    assert (await h.store.get(h.actor, rid))["run"]["status"] == "paused"
    assert not (await h.store.get(h.actor, rid))["run"]["owner_flag"]


async def test_invalid_json_and_acceptance_failures_share_one_episode_without_replanning(recovery_h):
    h = recovery_h
    team = {**TEAM, "topology": LOOP_TOPOLOGY, "audit_bindings": ["eye"]}
    await h.save(definition=team)
    contract = {"criteria": [{"id": "final", "output_kind": "answer", "checks": {"required_sections": ["Conclusion"]}}]}
    _, started = await h.start(acceptance=contract)
    rid = started["run"]["id"]; row = await h.store.claim("controller")
    calls = []
    async def send(binding, prompt):
        calls.append(prompt)
        if binding == AUDITOR:
            return {"text": advice(prompt)}
        if 'Return ONLY JSON {"tasks"' in prompt:
            return {"text": canonical({"tasks": [{"id": "task", "instruction": "answer", "dependencies": [], "output_kind": "answer", "criteria_ids": ["final"]}]})}
        return {"text": canonical({"content": "Missing section", "citations": []})}
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["reason"] == "guided_retry_failed" and run["calls_used"] == 6
    assert sum('Return ONLY JSON {"tasks"' in p for p in calls) == 1
    assert not run["accepted_outputs"] and not run["independently_verified"]


async def test_persisted_retry_after_is_bounded_and_cannot_dispatch_early(recovery_h, monkeypatch):
    from core.execution.failure import retry_delay
    h = recovery_h
    monkeypatch.setattr("core.execution.failure_store.retry_delay", retry_delay)
    rid, row = await admitted(h)
    step = (await h.store.steps(row))[0]
    attempt = await h.store.dispatch(row, 0, MODEL["one"], "source")
    episode = await h.store.record_failure(row, step, attempt, MODEL["one"],
                    ExecutionError("provider_error", 502, {"status": 429, "retry_after": "999999"}))
    remaining = await h.store.failure_wait(row, episode["id"])
    assert 55 <= remaining <= 60
    with pytest.raises(ExecutionError, match="retry_not_due"):
        await h.store.reserve_attempt(row, 0, MODEL["one"], "retry", episode_id=episode["id"], purpose="basic")
    assert (await h.store.get(h.actor, rid))["run"]["calls_used"] == 1


async def test_agents_can_read_only_their_own_scoped_run_flags(recovery_h):
    h = recovery_h
    agent = replace(h.actor, actor="scoped-agent", token_type="agent", scope={"may_touch": ["one/**"]})
    await h.service.save(agent, {"definition": TEAM, "expected_revision": None, "request_key": "agent-team"})
    started = await h.service.start(agent, {"contract_version": 2, "team": TEAM["id"], "team_revision": 1,
        "task": "Inspect SOURCE-ORIGINAL", "evidence_ids": ["SOURCE-ORIGINAL"], "acceptance": "preserve source",
        "request_key": "agent-run", "budget": {"start_calls": 100, "wall_mode": "none", "wall_seconds": None}})
    rid = started["run"]["id"]; row = await h.store.claim("controller")
    send, calls = transport(h)
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    with pytest.raises(ExecutionError, match="run_id_required"):
        await h.store.flags(agent, {})
    flags = await h.store.flags(agent, {"run_id": rid})
    assert len(flags["items"]) == 1
    flag_id = flags["items"][0]["id"]
    assert (await h.store.flag_get(agent, {"flag_id": flag_id}))["flag"]["run_id"] == rid
    assert (await h.store.flags(h.actor, {}))["items"][0]["id"] == flag_id
    with pytest.raises(ExecutionError, match="flag_not_found"):
        await h.store.flag_get(replace(agent, scope={"may_touch": ["other/**"]}), {"flag_id": flag_id})


async def test_parallel_sibling_is_settled_unknown_when_failure_pauses_group(recovery_h):
    h = recovery_h
    await h.save(definition={**TEAM, "topology": LOOP_TOPOLOGY, "max_parallelism": 2})
    contract = {"criteria": [{"id": cid, "output_kind": "answer", "checks": {}} for cid in ("a", "b")]}
    _, started = await h.start(acceptance=contract)
    rid = started["run"]["id"]; row = await h.store.claim("controller")
    closed = asyncio.Event()
    async def send(binding, prompt):
        if 'Return ONLY JSON {"tasks"' in prompt:
            return {"text": canonical({"tasks": [{"id": cid, "instruction": "task-" + cid, "dependencies": [],
                      "output_kind": "answer", "criteria_ids": [cid]} for cid in ("a", "b")]})}
        if '"instruction":"task-b"' in prompt:
            try:
                await asyncio.Event().wait()
            finally:
                closed.set()
        raise ExecutionError("provider_error", 502, {"status": 503})
    await Executor(h.store, h.service, send, poll=.001).execute(row)
    run = (await h.store.get(h.actor, rid))["run"]
    assert run["reason"] == "second_eye_unavailable" and run["unknown_calls"] == 1
    assert closed.is_set() and await h.pg.fetchval("SELECT count(*) FROM agent_attempts WHERE state='dispatched'") == 0
