"""S4g app-copy contracts. Run in WSL/Linux with real fcntl; no network."""
import sys
from types import SimpleNamespace

import pytest

from test_owner_session_broker import setup, confirm  # noqa: F401

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="requires Linux fcntl; run in WSL")


@pytest.fixture
def ladder_flags(setup):
    mod, broker, state, session, stream, transport = setup
    operations = {op: {"contract_versions": [2], "available": True, "implemented": True}
                  for op in ("flags.list", "flags.get", "flags.resolve", "runs.start")}
    session["allowed_operations"].append("flags.resolve")
    original = broker._rest

    def rest(method, path, **kwargs):
        result = original(method, path, **kwargs)
        if path == "/capabilities":
            result["operations"].update(operations)
        return result

    broker._rest = rest
    yield mod, broker, state, session, stream, operations


def test_job_fields_and_large_integers_forwarded_literally(ladder_flags):
    mod, broker, state, session, stream, operations = ladder_flags
    raw = {"contract_version": 2, "team": "configured-team", "team_revision": 1,
           "task": "supplied evidence", "request_key": "same-start",
           "budget": {"start_calls": 50, "wall_mode": "none", "wall_seconds": None,
                      "max_job_attempts": "9007199254740993", "job_seconds_budget": "9223372036854775807"},
           "job_coverage": {"profiles": {"acceptance": "a" * 64}, "workspaces": {"fixture": ["."]},
                            "attachments": [], "max_job_attempts": "9007199254740993",
                            "job_seconds_budget": "9223372036854775807", "allow_cli": False}}
    assert broker.op_runs_start(raw)["received"] == raw
    confirm(broker)
    grant = {"contract_version": 2, "proposal_id": "p", "proposal_revision": "9007199254740993",
             "expected_revision": None, "request_key": "same-issue", "coverage_kind": "task",
             "allow_long_runs": True, "auto_escalate": False, "call_ceiling": None, "expires_at": None,
             "allow_extended_jobs": True, "allow_custom_job_limit": True,
             "job_seconds_ceiling": "9223372036854775807", "job_override_seconds": 7200,
             "job_auto_escalate": False}
    assert broker.op_grants_issue(grant)["received"] == grant
    assert state["hits"][-1][0] == state["bearer"]
    for value in (True, 0, 1.5, "01", "9223372036854775808"):
        invalid = {**raw, "budget": {**raw["budget"], "max_job_attempts": value}}
        with pytest.raises(mod.BrokerError):
            broker.op_runs_start(invalid)


def test_flags_exact_routes_read_transport_and_resolution_never_resumes(ladder_flags):
    mod, broker, state, session, stream, operations = ladder_flags
    broker.op_flags_list({"contract_version": 2, "run_id": "r", "status": "all", "limit": 50})
    assert state["hits"][-1][1:3] == ("GET", "/agent-services/flags")
    assert state["hits"][-1][0] != state["bearer"]
    broker.op_flags_get({"contract_version": 2, "flag_id": "f", "after_event_seq": "9007199254740993", "job_cursor": "execution-1"})
    assert state["hits"][-1][2] == "/agent-services/flags/f"
    assert state["hits"][-1][3]["params"]["after_event_seq"] == "9007199254740993"
    raw = {"contract_version": 2, "flag_id": "f", "expected_revision": "9223372036854775807",
           "request_key": "same-resolution", "decision": "resume_with_guidance", "guidance": "Inspected receipts"}
    with pytest.raises(mod.BrokerError, match="owner_confirmation_required"):
        broker.op_flags_resolve(raw)
    confirm(broker)
    before = len(state["hits"])
    broker.op_flags_resolve(raw)
    assert state["hits"][-1][0] == state["bearer"]
    assert state["hits"][-1][1:3] == ("POST", "/agent-services/flags/f/resolve")
    assert state["hits"][-1][3]["body"] == {k: v for k, v in raw.items() if k != "flag_id"}
    assert not any(h[2].endswith("/resume") or h[2].endswith("/cancel") for h in state["hits"][before:])
    for key, value in (("expected_revision", "01"), ("decision", "resume"), ("guidance", "x" * 4001), ("owner_bearer", "forbidden")):
        with pytest.raises(mod.BrokerError):
            broker.op_flags_resolve({**raw, key: value})


@pytest.mark.parametrize("missing", ["flags.list", "flags.get", "flags.resolve"])
def test_each_flag_operation_negotiates_independently(ladder_flags, missing):
    mod, broker, state, session, stream, operations = ladder_flags
    operations[missing] = {}  # other operations remain advertised
    confirm(broker)
    calls = {"flags.list": lambda: broker.op_flags_list({"contract_version": 2, "run_id": "r"}),
             "flags.get": lambda: broker.op_flags_get({"contract_version": 2, "flag_id": "f"}),
             "flags.resolve": lambda: broker.op_flags_resolve({"contract_version": 2, "flag_id": "f", "expected_revision": 1,
                                                              "request_key": "same", "decision": "inspected", "guidance": "Checked"})}
    with pytest.raises(mod.BrokerError, match="client_upgrade_required"):
        calls[missing]()
    for op, call in calls.items():
        if op != missing:
            call()


def test_flags_owner_denial_and_no_credentials_in_output_or_ledger(ladder_flags, capsys):
    mod, broker, state, session, stream, operations = ladder_flags
    touched = []
    broker.ledger = SimpleNamespace(record=lambda *a: touched.append(a), rows=lambda: [])
    broker.identity["token_type"] = "agent"
    with pytest.raises(mod.BrokerError, match="owner_portal_required"):
        confirm(broker)
    with pytest.raises(mod.BrokerError, match="owner_confirmation_required"):
        broker.op_flags_resolve({"contract_version": 2, "flag_id": "f", "expected_revision": 1,
                                "request_key": "same", "decision": "inspected", "guidance": "Checked"})
    broker.identity["token_type"] = "portal"
    result = confirm(broker)
    broker.sink.reply({"value": result})
    # The sink scrubs the registered owner bearer; submitted factors are never emitted at all.
    broker.sink.event("flag", {"owner_bearer": state["bearer"], "text": "flag detail"})
    captured = capsys.readouterr()
    output = stream.getvalue().decode() + captured.out + captured.err
    for secret in (state["bearer"], "synthetic-password", "123456"):
        assert secret not in output
    assert not touched
