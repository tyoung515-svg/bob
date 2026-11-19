"""Identical v2 fixtures for the source broker and its actual shipping snapshot.

The sibling app copy is intentionally NOT edited by this backend brief.
"""
import importlib.util
import os
from contextlib import nullcontext
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from types import ModuleType
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(params=["source", "packaged", "app-copy"])
def broker(request, tmp_path, monkeypatch):
    monkeypatch.setenv("BOB_PORTAL_STATE_DIR", str(tmp_path))
    path = ROOT / "portal-broker/portal-broker.py"
    if request.param == "app-copy":
        # The sibling app repository's own broker copy must satisfy the same fixtures.
        app_source = Path(os.environ.get("BOB_APP_BROKER", str(ROOT.parent / "app/portal/portal-broker.py")))
        if not app_source.exists():
            pytest.skip("Sibling app copy absent; source/packaged broker tests still required")
        if "def op_owner_confirm" not in app_source.read_text(encoding="utf-8"):
            pytest.skip("Sibling app copy predates the owner-session contract")
        if "def op_policy_get" not in app_source.read_text(encoding="utf-8"):
            pytest.skip("Sibling app copy predates the settings/policy contract")
        if any("def op_" + op not in app_source.read_text(encoding="utf-8")
               for op in ("flags_list", "flags_get", "flags_resolve")):
            pytest.skip("Sibling app copy predates the flags contract")
        path = app_source
    if request.param == "packaged":
        snapshot = tmp_path / "snapshot"
        subprocess.run([sys.executable, str(ROOT / "deploy/runtime/snapshot.py"), str(ROOT), str(snapshot)], check=True)
        path = snapshot / "portal-broker/portal-broker.py"
        assert path.read_bytes() == (ROOT / "portal-broker/portal-broker.py").read_bytes()
        assert (snapshot / "migrations/2026-11-03_agent_runs.sql").exists()
        assert (snapshot / "bobclaw-core/core/execution/executor.py").exists()
        for relative in ("migrations/2026-11-03_agent_runs_loop.sql", "bobclaw-core/core/execution/budgets.py",
                         "bobclaw-core/core/execution/loop_store.py", "bobclaw-core/core/execution/planning.py",
                         "bobclaw-core/core/execution/recovery.py", "bobclaw-core/core/execution/call_guard.py"):
            assert (snapshot / relative).read_bytes() == (ROOT / relative).read_bytes()
        for relative in ("migrations/2026-11-03_owner_grants.sql", "bobclaw-core/core/execution/grants.py",
                         "bobclaw-core/core/execution/owner.py", "bobclaw-gateway/owner_sessions.py",
                         "migrations/2026-11-03_z_failure_recovery.sql", "bobclaw-core/core/execution/failure.py",
                         "bobclaw-core/core/execution/failure_store.py"):
            assert (snapshot / relative).read_bytes() == (ROOT / relative).read_bytes()
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
            return {"operations": {op: {"contract_versions": [2]} for op in ("runs.start", "runs.resume", "budgets.set")}}
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


def test_s4d_owner_operations_preserve_revision_and_budget_and_negotiate(broker):
    module, instance, hits = broker
    # S3b retains exact forwarding on a distinct, confirmed owner transport.
    instance._owner_transport = instance.transport
    instance._owner_session = {"active": True, "expires_at": module.time.time()+300}
    resume = {"contract_version":2,"run_id":"run-123","expected_revision":1234,"request_key":"resume-key",
              "budget":{"start_calls":100,"wall_mode":"none","wall_seconds":None}}
    instance.op_runs_resume(resume)
    assert hits[-1][1] == "/agent-services/runs/run-123/resume"
    assert hits[-1][2]["body"] == {k:v for k,v in resume.items() if k != "run_id"}
    pool = {"contract_version":2,"project_id":"p","call_limit":2000,"expected_revision":None,"request_key":"pool-key"}
    instance.op_budgets_set(pool)
    assert hits[-1][1] == "/agent-services/budgets" and hits[-1][2]["body"] == pool
    instance.op_runs_get({"contract_version":2,"run_id":"run-123","receipt_cursor":1199})
    assert hits[-1][2]["params"]["receipt_cursor"] == 1199
    instance._rest = lambda *a,**k: {"operations":{"runs.start":{"contract_versions":[2]}}}
    with pytest.raises(module.BrokerError,match="client_upgrade_required"):
        instance.op_runs_resume(resume)
    with pytest.raises(module.BrokerError):
        instance.op_budgets_set({**pool,"sprint_id":"no-resource"})


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
    # Mirrors the app broker test: the packaged broker must expose v2 to the app.
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
