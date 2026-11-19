"""Pure matcher, integer wire and authenticated-envelope adversarial tests."""
from copy import deepcopy
import time

import pytest

from core.execution.grants import grant_matches
from core.execution.budgets import task_digest
from core.execution.models import ExecutionError, decode_integers, digest, wire_integers
from core.permissions import execution_vouch, verify_execution_vouch


def fixtures():
    row = {"id": "planned", "boundary": "original-actor-scope", "request": {"task": "S1", "project_id": "p", "evidence_ids": ["S1"]},
           "snapshot": {"team": {"id": "team", "revision": 1, "permitted_tools": [], "job_profiles": []}, "models": {"worker": "exact-model"}},
           "run_authority": {"credential_epoch": 1}}
    grant = {"run_id": "planned", "project_id": "p", "authority_boundary": row["boundary"], "task_digest": task_digest(row["request"]),
             "team_digest": digest(row["snapshot"]["team"]), "models_digest": digest(row["snapshot"]["models"]),
             "matcher_version": 2, "coverage_kind": "task", "credential_epoch": 1}
    return row, grant


def test_v1_project_grants_keep_exact_task_semantics_and_v2_explicitly_covers_future():
    row, grant = fixtures()
    grant.update(run_id=None, matcher_version=1)
    assert grant_matches(grant, row)
    row["request"]["task"] = "different future task"
    assert not grant_matches(grant, row)
    grant.update(matcher_version=2, coverage_kind="project")
    assert grant_matches(grant, row)
    grant.update(coverage_kind="task", task_digest="")
    assert not grant_matches(grant, row)  # empty digest never means wildcard


@pytest.mark.parametrize("change", ["actor", "project", "team", "model", "tools", "jobs", "epoch"])
def test_project_grant_excludes_wider_or_changed_boundaries(change):
    row, grant = fixtures()
    grant.update(run_id=None, coverage_kind="project")
    if change == "actor":
        row["boundary"] = "other-actor-or-scope"
    elif change == "project":
        row["request"]["project_id"] = "foreign"
    elif change == "team":
        row["snapshot"]["team"]["revision"] = 2
    elif change == "model":
        row["snapshot"]["models"]["worker"] = "unapproved-model"
    elif change in {"tools", "jobs"}:
        row["snapshot"]["team"]["permitted_tools" if change == "tools" else "job_profiles"] = ["new"]
    else:
        row["run_authority"]["credential_epoch"] = 2
    assert not grant_matches(grant, row)


@pytest.mark.parametrize("bad", [True, 1.0, 9007199254740992, "01", "+2", "-1", "1.0", "1e3", "9223372036854775808", " 12"])
def test_reject_rounded_noncanonical_or_overflow_integers(bad):
    with pytest.raises(ExecutionError, match="invalid_integer"):
        decode_integers({"call_ceiling": bad})


def test_int64_roundtrip_uses_decimal_strings_and_keeps_null():
    value = {"call_ceiling": 9223372036854775807, "budget": {"start_calls": 1200}, "job_seconds_ceiling": None}
    wire = wire_integers(value)
    assert wire["call_ceiling"] == "9223372036854775807"
    assert decode_integers(wire) == value


@pytest.mark.parametrize("operation", ["grants.prepare", "grants.issue", "budgets.remove", "runs.resume"])
def test_vouch_binds_exact_session_provenance_epoch_and_operation(operation):
    now = int(time.time())
    envelope = {"operation": operation, "args": {"request_key": "key"}, "iat": now, "exp": now+30,
                "authority": {"session_kind": "owner_stepup", "credential_epoch": 1, "portal_credential_id": "portal", "broker_link_nonce": "link"}}
    secret = "fixture-vouch-secret"
    signed = execution_vouch(envelope, secret)
    assert verify_execution_vouch(envelope, signed, secret)
    for key in envelope["authority"]:
        changed = deepcopy(envelope)
        changed["authority"][key] = 2 if key == "credential_epoch" else "forged"
        assert not verify_execution_vouch(changed, signed, secret)
    assert not verify_execution_vouch(envelope, "0"*64, secret)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("allow_extended_jobs", True), ("allow_custom_job_limit", True), ("job_seconds_ceiling", 7200)])
async def test_job_grant_permissions_require_durable_store(field, value):
    from core.execution.store import Store
    store = Store(None, "fixture")
    args = {"proposal_id": "p", "proposal_revision": 1, "expected_revision": None, "request_key": "key", "coverage_kind": "task",
            "allow_long_runs": True, "auto_escalate": True, "call_ceiling": None, "expires_at": None, field: value}
    with pytest.raises(ExecutionError, match="execution_storage_unavailable"):
        await store.issue_grant(None, args)
