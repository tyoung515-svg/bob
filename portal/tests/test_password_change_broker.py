"""S3h app-copy password tests. Run on Linux/WSL with real fcntl.

These fake gateway/receipt fixtures prove app projection and one-shot dispatch.
They do not replace the backend's Linux helper, real SQLite/TOTP or crash tests.
"""
import io
import json
from pathlib import PurePosixPath
import socket
import stat
import struct
import sys
from types import SimpleNamespace

import pytest

from test_owner_session_broker import setup as owner_setup, confirm

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="requires Linux fcntl; run in WSL")
FACTORS = ("S3H-OLD-PASSWORD-CANARY", "S3H-NEW-PASSWORD-CANARY", "654321")


def args(**changes):
    return dict(contract_version=2, current=FACTORS[0], new=FACTORS[1],
                totp=FACTORS[2], request_key="password-once") | changes


@pytest.fixture
def password(owner_setup):
    mod, broker, state, session, stream, _ = owner_setup
    confirm(broker)
    state.update(password_calls=[], mode="ready", receipt=None)
    cap = dict(contract_versions=[2], available=True, implemented=True)
    original = broker.transport.rest

    def service(method, path, **kwargs):
        if path == "/capabilities":
            value = original(method, path, **kwargs)
            if cap:
                value["operations"]["auth.change_password"] = cap
            return value
        if path != "/settings/password":
            return original(method, path, **kwargs)
        body = kwargs["body"]
        state["password_calls"].append((method, path))  # never retain factors
        assert kwargs["owner_headers"] == {"X-Bob-Broker-Link": broker._owner_link_nonce}
        assert body == args() | {"request_key": body["request_key"]}
        result = dict(changed=True, restarted=state["mode"] != "restart_pending")
        state["receipt"] = result
        if state["mode"] in {"wrong", "spent_totp", "invalid_totp"}:
            return dict(changed=False, restarted=False)
        if state["mode"] == "dropped":
            raise mod.BrokerError("transport_unavailable", " ".join(FACTORS))
        if state["mode"] == "rule":
            raise mod.BrokerError("gateway_http_422", "invalid_password")
        if state["mode"] == "expiry":
            raise mod.BrokerError("owner_session_expired", " ".join(FACTORS))
        if state["mode"] == "echo":
            raise RuntimeError(" ".join(FACTORS) + state["bearer"])
        if state["mode"] == "malformed":
            return dict(changed=False, restarted=True, password=FACTORS[1])
        if state["mode"] == "throttled":
            raise mod.BrokerError("gateway_http_429", " ".join(FACTORS), 30)
        return dict(result, current=FACTORS[0], new=FACTORS[1], totp=FACTORS[2],
                    owner_bearer=state["bearer"], credential_epoch=3)

    broker.transport.rest = service
    state["hits"].clear()
    return mod, broker, state, stream, cap


@pytest.fixture
def receipt_socket(password, monkeypatch):
    mod, broker, state, _, _ = password
    monkeypatch.setenv("BOB_SETTINGS_ID", "a" * 12)
    state["receipt_phase"] = "ready"
    requests = []

    class InstalledPath:
        def __init__(self, value):
            self.path = PurePosixPath(value)

        def __truediv__(self, value):
            return InstalledPath(self.path / value)

        @property
        def parents(self):
            return tuple(InstalledPath(p) for p in self.path.parents)

        def lstat(self):
            return SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o755)

        def __str__(self):
            return str(self.path)

    class ReceiptSocket:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def settimeout(self, seconds):
            assert seconds == 10

        def connect(self, path):
            assert path == "/run/bob-settings/" + "a" * 12 + "/receipt.sock"

        def getsockopt(self, *args):
            return struct.pack("3i", 1, 0, 0)

        def sendall(self, raw):
            requests.append(json.loads(raw))

        def makefile(self, mode):
            return io.BytesIO(json.dumps(dict(
                operation_id="password-op", phase=state["receipt_phase"],
                result=state["receipt"], password=FACTORS[1],
                owner_bearer=state["bearer"])).encode() + b"\n")

    monkeypatch.setattr(mod, "Path", InstalledPath)
    monkeypatch.setattr(socket, "socket", lambda *a: ReceiptSocket())
    return requests


@pytest.mark.parametrize("mode,expected", [
    ("ready", dict(changed=True, restarted=True)),
    ("restart_pending", dict(changed=True, restarted=False)),
    ("wrong", dict(changed=False, restarted=False)),
    ("spent_totp", dict(changed=False, restarted=False)),
    ("invalid_totp", dict(changed=False, restarted=False)),
])
def test_exact_outcomes_single_combined_proof_and_session_discard(password, mode, expected):
    _, broker, state, _, _ = password
    state["mode"] = mode
    assert broker.op_password_change(args()) == expected
    assert state["password_calls"] == [("POST", "/settings/password")]
    assert not [hit for hit in state["hits"] if hit[2] == "/auth/owner/confirm"]
    assert broker._owner_transport is None
    assert broker._owner_session == {"active": False}


def test_no_password_totp_or_bearer_in_replies_hello_ledger_logs(password, tmp_path, capsys):
    mod, broker, state, stream, _ = password
    touched = []
    broker._ledger_remember = lambda *a: touched.append(a)
    frames = [
        dict(id=1, op="auth.change_password", args=args()),
        dict(id=2, op="auth.change_password", args=args()),
        dict(id=3, op="hello", args={}),
        dict(id=4, op="auth.change_password", args=args(**{FACTORS[1]: True})),
        dict(id=5, op="auth.change_password", args=args(), **{FACTORS[0]: True}),
    ]
    mod.serve(broker, broker.sink, io.BytesIO(("\n".join(json.dumps(f) for f in frames) + "\n").encode()))
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert lines[0]["value"] == dict(changed=True, restarted=True)
    assert lines[1]["error"]["code"] == "request_key_used"
    assert lines[3]["ok"] is False and lines[4]["ok"] is False
    captured = capsys.readouterr()
    output = stream.getvalue().decode() + captured.out + captured.err
    for secret in (*FACTORS, state["bearer"]):
        assert secret not in output
        assert secret not in mod._OWNER_SECRETS
        for file in tmp_path.rglob("*"):
            if file.is_file():
                assert secret.encode() not in file.read_bytes()
    assert not touched
    assert state["password_calls"] == [("POST", "/settings/password")]


@pytest.mark.parametrize("mode,code", [
    ("dropped", "password_outcome_unknown"), ("malformed", "invalid_receipt"),
    ("echo", "password_outcome_unknown"), ("rule", "invalid_password"),
    ("expiry", "owner_session_expired"), ("throttled", "authentication_throttled"),
])
def test_fixed_error_projection_never_retries(password, mode, code):
    mod, broker, state, _, _ = password
    state["mode"] = mode
    with pytest.raises(mod.BrokerError) as error:
        broker.op_password_change(args())
    assert error.value.code == code
    frame = json.dumps(mod._error_frame(1, error.value))
    for secret in (*FACTORS, state["bearer"]):
        assert secret not in frame
    if mode == "throttled":
        assert error.value.retry_after == 30
    assert len(state["password_calls"]) == 1
    assert broker._owner_transport is None
    # Even changed factor bodies cannot reuse an admitted request key.
    with pytest.raises(mod.BrokerError, match="request_key_used"):
        broker.op_password_change(args(new="another-password"))
    assert len(state["password_calls"]) == 1


@pytest.mark.parametrize("capability", [
    {}, dict(contract_versions=[1], available=True),
    dict(contract_versions=[2], available=False, reason="credential_generation_required"),
])
def test_operation_capability_is_independent_and_required(password, capability):
    mod, broker, state, _, cap = password
    cap.clear()
    cap.update(capability)
    with pytest.raises(mod.BrokerError) as error:
        broker.op_password_change(args())
    assert error.value.code in {"client_upgrade_required", "operation_unavailable"}
    assert not state["password_calls"]


@pytest.mark.parametrize("changes", [
    dict(contract_version=True), dict(contract_version=1), dict(contract_version="2"),
    dict(request_key="../key"), dict(request_key="two words"), dict(current=False),
    dict(new="x" * 4097), dict(totp="x" * 17), dict(owner="forged"),
])
def test_closed_password_schema_no_dispatch_or_echo(password, changes):
    mod, broker, state, _, _ = password
    with pytest.raises(mod.BrokerError) as error:
        broker.op_password_change(args(**changes))
    assert not state["password_calls"]
    for secret in FACTORS:
        assert secret not in json.dumps(mod._error_frame(1, error.value))


@pytest.mark.parametrize("identity", ["agent", "human"])
def test_agents_and_ordinary_human_credentials_cannot_supply_owner_context(password, identity):
    mod, broker, state, _, _ = password
    broker.identity["token_type"] = identity
    with pytest.raises(mod.BrokerError, match="owner_portal_required"):
        broker.op_password_change(args())
    assert not state["password_calls"]


@pytest.mark.parametrize("phase,result", [
    ("prepared", None), ("committed", None), ("restart-pending", None),
    ("ready", dict(changed=True, restarted=True)),
    ("failed", dict(changed=True, restarted=False)),
    ("failed", dict(changed=False, restarted=False)),
])
def test_gateway_down_receipt_only_without_owner_or_credentials(password, receipt_socket, phase, result):
    _, broker, state, _, _ = password
    broker._forget_owner()
    broker._rest = lambda *a, **kw: pytest.fail("Receipt lookup must not contact the gateway")
    broker._portal_owner_rest = lambda *a, **kw: pytest.fail("Credentials must never be resent")
    state["receipt_phase"], state["receipt"] = phase, result
    safe = broker.op_settings_status({"request_key": "password-once"})
    assert safe == dict(operation_id="password-op", phase=phase, **({"result": result} if result else {}))
    assert receipt_socket == [dict(version=1, op="status", request_key="password-once")]


def test_dropped_reply_after_commit_resolved_via_receipt_without_resend(password, receipt_socket):
    mod, broker, state, _, _ = password
    state["mode"] = "dropped"
    with pytest.raises(mod.BrokerError, match="password_outcome_unknown"):
        broker.op_password_change(args())
    assert state["receipt"] == dict(changed=True, restarted=True)
    broker._rest = lambda *a, **kw: pytest.fail("Gateway is down")
    result = broker.op_settings_status({"request_key": "password-once"})
    assert result["result"] == dict(changed=True, restarted=True)
    assert len(state["password_calls"]) == 1
    assert receipt_socket == [dict(version=1, op="status", request_key="password-once")]


def test_no_receipt_or_invalid_result_remains_unknown(password, receipt_socket):
    mod, broker, state, _, _ = password
    for result in [dict(changed=False, restarted=True), dict(changed="yes", restarted=False)]:
        state["receipt_phase"], state["receipt"] = "ready", result
        with pytest.raises(mod.BrokerError, match="settings_receipt_unavailable"):
            broker.op_settings_status({"request_key": "password-once"})
    state["receipt_phase"] = "absent"
    with pytest.raises(mod.BrokerError, match="settings_receipt_unavailable"):
        broker.op_settings_status({"request_key": "password-once"})
