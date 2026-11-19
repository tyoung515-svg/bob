"""S4B broker fixtures copied unchanged in assertions, routed to the app broker.

Linux uses real fcntl. Windows is import-only with fail-closed legacy locking.
Source: backend b7d5157, bobclaw-gateway/tests/test_execution_broker_v2.py.
"""
import importlib.util
from contextlib import nullcontext
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from types import ModuleType
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["app"])
def broker(request, tmp_path, monkeypatch):
    monkeypatch.setenv("BOB_PORTAL_STATE_DIR", str(tmp_path))
    path = ROOT / "portal-broker.py"
    spec = importlib.util.spec_from_file_location("_s4b_broker_" + request.param, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    # v2 never touches the legacy ledger. On Windows permit import only, with a
    # fail-closed flock stub: any accidental ledger access fails this test. Linux
    # uses real fcntl; this shim makes no claim about POSIX file-lock behavior.
    fcntl = ModuleType("fcntl")
    fcntl.LOCK_EX, fcntl.LOCK_SH, fcntl.LOCK_UN = 2, 1, 8
    def forbidden_lock(*args):
        raise AssertionError("v2 must not touch legacy ledger locking")
    fcntl.flock = forbidden_lock
    context = patch.dict(sys.modules, {"fcntl": fcntl}) if sys.platform == "win32" else nullcontext()
    with context:
        spec.loader.exec_module(module)
    hits = []

    def rest(method, path, **kwargs):
        hits.append((method, path, kwargs))
        if path == "/capabilities":
            return {"operations": {"runs.start": {"contract_versions": [2]}}}
        return {"source": "server", "items": [], "received": kwargs.get("body")}

    instance = module.Broker(SimpleNamespace(rest=rest), {"sub": "fixture-owner"}, SimpleNamespace())
    try:
        yield module, instance, hits
    finally:
        sys.modules.pop(spec.name, None)


def test_v2_operations_forward_exact_keys_and_null_budget(broker):
    _, instance, hits = broker
    start = {"contract_version": 2, "team": "any-configured-team", "team_revision": 3, "task": "source",
             "request_key": "caller-key", "budget": {"start_calls": 1000, "wall_mode": "none", "wall_seconds": None}}
    assert instance.op_runs_start(start)["received"] == start  # no four-call/900s clamp; server returns confirmation
    assert hits[-1][2]["body"] == start
    assert instance.op_runs_list({"contract_version": 2})["source"] == "server"
    instance.op_runs_get({"contract_version": 2, "run_id": "run-123", "after_event_seq": 4})
    assert hits[-1][2]["params"] == {"contract_version": 2, "after_event_seq": 4}
    instance.op_runs_cancel({"contract_version": 2, "run_id": "run-123", "request_key": "cancel-key"})
    assert hits[-1][2]["body"] == {"contract_version": 2, "request_key": "cancel-key"}
    instance.op_teams_save({"definition": {"id": "draft"}, "expected_revision": None, "request_key": "save-key"})
    assert hits[-1][1] == "/teams" and hits[-1][2]["body"]["expected_revision"] is None
    instance.op_teams_list({})
    assert hits[-1][2]["params"] == {"contract_version": 2}


def test_old_server_and_missing_keys_do_not_send_reduced_retry(broker):
    module, instance, hits = broker
    for args in ({"contract_version": 2, "team": "x", "task": "x"},
                 {"contract_version": True, "team": "x", "task": "x"}):
        with pytest.raises(module.BrokerError):
            instance.op_runs_start(args)
    assert not hits
    instance._rest = lambda *a, **k: {}
    with pytest.raises(module.BrokerError) as exc:
        instance.op_runs_list({"contract_version": 2})
    assert exc.value.code == "client_upgrade_required" and not hits


def test_catalog_projects_execution_metadata_and_exact_bindings(broker):
    _, instance, hits = broker
    caps = {"contract_versions": [2], "start_calls": [10, 50, 100, 200],
            "execution_enabled": False, "blocked_reasons": ["execution_storage_or_executor_unavailable"]}
    def rest(method, path, **kwargs):
        hits.append((method, path, kwargs))
        if path == "/agent-services/catalog?contract_version=2":
            return {"capabilities": {"execution_v2": caps}, "model_faces": [{"id": "configured-binding"}]}
        return [] if path != "/capabilities" else {}
    instance._rest = rest
    result = instance.op_catalog({})
    assert result["capabilities"]["execution_v2"] == caps
    assert result["capabilities"]["execution_models"] == [{"id": "configured-binding"}]
    assert len(hits) == 5


def test_v2_control_reads_do_not_depend_on_model_catalog(broker):
    module, instance, hits = broker
    def rest(method, path, **kwargs):
        hits.append((method, path, kwargs))
        if path == "/capabilities":
            return {"operations": {"runs.start": {"contract_versions": [2]}}}
        if path.startswith("/agent-services/runs"):
            return {"source": "server", "items": [], "next_cursor": "opaque-next"}
        raise module.BrokerError("transport_unavailable", "model catalog down")
    instance._rest = rest
    assert instance.op_runs_list({"contract_version": 2, "cursor": "opaque", "limit": 20})["next_cursor"] == "opaque-next"
    assert hits[-1][2]["params"]["cursor"] == "opaque"
    assert not any("catalog" in path for _, path, _ in hits)


def test_catalog_can_expose_execution_during_model_registry_outage(broker):
    module, instance, _ = broker
    def rest(method, path, **kwargs):
        if path == "/agent-services/catalog?contract_version=2":
            return {"capabilities": {"execution_v2": {"contract_versions": [2]}}}
        raise module.BrokerError("transport_unavailable", "model registry down")
    instance._rest = rest
    result = instance.op_catalog({})
    assert result["models"] == []
    assert result["capabilities"]["execution_v2"]["contract_versions"] == [2]


def test_legacy_list_is_explicitly_labelled(broker):
    _, instance, _ = broker
    instance.ledger = SimpleNamespace(rows=lambda: [])
    assert instance.op_runs_list({})["source"] == "legacy_portal_ledger"


@pytest.mark.parametrize("budget", [
    {"start_calls": True, "wall_mode": "none", "wall_seconds": None},
    {"start_calls": 10.5, "wall_mode": "none", "wall_seconds": None},
    {"start_calls": 100, "wall_mode": "fixed", "wall_seconds": None},
    {"start_calls": 100, "wall_mode": "none", "wall_seconds": 900},
])
def test_invalid_v2_budget_does_not_dispatch(broker, budget):
    module, instance, hits = broker
    with pytest.raises(module.BrokerError):
        instance.op_runs_start({"contract_version": 2, "team": "configured", "team_revision": 1,
                                "task": "source", "request_key": "same-key", "budget": budget})
    assert hits == []


@pytest.mark.parametrize("op", ["runs.resume", "budgets.set"])
def test_owner_operations_negotiate_exact_operation_and_forward_unchanged(broker, op):
    _, instance, hits = broker
    def rest(method, path, **kwargs):
        hits.append((method, path, kwargs))
        if path == "/capabilities":
            return {"operations": {op: {"contract_versions": [2]}}}
        return {"received": kwargs["body"]}
    instance._rest = rest
    instance._owner_transport = SimpleNamespace(rest=rest)
    instance._owner_session = {"active": True, "expires_at": time.time() + 240}
    args = {"contract_version": 2, "expected_revision": 5, "request_key": "keep-same-key"}
    if op == "runs.resume":
        args.update(run_id="run-123", budget={"start_calls": 100, "wall_mode": "none", "wall_seconds": None})
        result = instance.op_runs_resume(args)
        assert hits[-1][1] == "/agent-services/runs/run-123/resume"
        assert result["received"] == {k: v for k, v in args.items() if k != "run_id"}
    else:
        args.update(project_id="project-123", call_limit=1000, expected_revision=None)
        result = instance.op_budgets_set(args)
        assert hits[-1][1] == "/agent-services/budgets"
        assert result["received"] == args
    assert len(hits) == 2
    assert hits[0][2]["params"] == {"operations": 1, "contract_version": 2}


@pytest.mark.parametrize("op", ["runs.resume", "budgets.set"])
def test_start_support_does_not_imply_owner_operation_support_or_resend(broker, op):
    module, instance, hits = broker
    # The fixture advertises runs.start only, as a 4.1 server does.
    args = {"contract_version": 2, "expected_revision": 2, "request_key": "same-key"}
    if op == "runs.resume":
        args["run_id"] = "run-123"
    else:
        args.update(project_id="project-123", call_limit=100)
    with pytest.raises(module.BrokerError) as exc:
        instance.handle({"id": 1, "op": op, "args": args})
    assert exc.value.code == "client_upgrade_required"
    assert len(hits) == 1 and hits[0][0:2] == ("GET", "/capabilities")


@pytest.mark.parametrize("revision", [True, 0, -1, 1.5, "02", 9223372036854775808])
def test_owner_operation_invalid_revisions_do_not_dispatch(broker, revision):
    module, instance, hits = broker
    with pytest.raises(module.BrokerError):
        instance.op_runs_resume({"contract_version": 2, "run_id": "run-123", "request_key": "key", "expected_revision": revision})
    with pytest.raises(module.BrokerError):
        instance.op_budgets_set({"contract_version": 2, "project_id": "project-123", "request_key": "key", "expected_revision": revision, "call_limit": 100})
    assert hits == []


def test_owner_denial_is_returned_once_without_legacy_resend(broker):
    module, instance, hits = broker
    def rest(method, path, **kwargs):
        hits.append((method, path, kwargs))
        if path == "/capabilities":
            return {"operations": {"runs.resume": {"contract_versions": [2]}}}
        raise module.BrokerError("gateway_http_404", "not_found")
    instance._rest = rest
    instance._owner_transport = SimpleNamespace(rest=rest)
    instance._owner_session = {"active": True, "expires_at": time.time() + 240}
    with pytest.raises(module.BrokerError) as exc:
        instance.op_runs_resume({"contract_version": 2, "run_id": "run-123", "request_key": "key", "expected_revision": 2})
    assert exc.value.detail == "not_found"
    assert len(hits) == 2 and hits[-1][0] == "POST"


@pytest.mark.parametrize("value", [None, True, 0, -1, 1.5, "0100", 9223372036854775808])
def test_project_pool_limit_is_positive_integer_not_nullable(broker, value):
    module, instance, hits = broker
    with pytest.raises(module.BrokerError):
        instance.op_budgets_set({"contract_version": 2, "project_id": "project-123", "request_key": "key", "expected_revision": None, "call_limit": value})
    assert not hits


def test_resume_revision_cannot_be_null_and_loop_start_keeps_acceptance(broker):
    module, instance, hits = broker
    with pytest.raises(module.BrokerError):
        instance.op_runs_resume({"contract_version": 2, "run_id": "run-123", "request_key": "key", "expected_revision": None})
    assert not hits
    args = {"contract_version": 2, "team": "configured-loop", "team_revision": 1, "task": "source",
            "request_key": "same-key", "evidence_ids": ["S1"],
            "acceptance": {"criteria": [{"id": "answer", "required": True, "output_kind": "answer", "checks": {"require_citations": True}}]},
            "budget": {"start_calls": 1, "wall_mode": "none", "wall_seconds": None}}
    assert instance.op_runs_start(args)["received"] == args
