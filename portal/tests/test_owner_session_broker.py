"""S3c app broker privacy/lifecycle tests. Run with real fcntl on Linux/WSL.

No gateway, credentials, provider calls or production files are used.
"""
import base64
import importlib.util
import io
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="requires Linux fcntl; run in WSL")
OPS = ("owner.begin", "owner.confirm", "owner.status", "owner.lock", "owner.logout",
       "runs.resume", "budgets.get", "budgets.set", "budgets.list", "budgets.remove",
       "grants.prepare", "grants.issue", "grants.get", "grants.list", "grants.revoke")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("BOB_PORTAL_STATE_DIR", str(tmp_path))
    spec = importlib.util.spec_from_file_location("_s3c_app_broker", Path(__file__).resolve().parents[1] / "portal-broker.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    state = {"hits": [], "confirm_error": None, "bearer": "bos1.synthetic-owner-secret", "owner": "fixture-owner"}
    session = {"active": True, "owner": "fixture-owner", "actor": "fixture-owner", "lane": "a" * 64,
               "expires_at": time.time() + 240, "credential_epoch": 1,
               "allowed_operations": [op for op in OPS if op not in {"owner.begin", "owner.confirm", "grants.prepare"}]}
    claims = base64.urlsafe_b64encode(json.dumps({"jti": "portal-jti"}).encode()).decode().rstrip("=")

    class Transport:
        base = "http://127.0.0.1:8000"
        timeout = 1

        def __init__(self, base=None, token="portal." + claims + ".signature", **kwargs):
            self._token = token
            self.headers = kwargs.get("owner_headers", {})

        def _bearer(self):
            return self._token

        def rest(self, method, path, **kwargs):
            state["hits"].append((self._token, method, path, kwargs))
            if path == "/capabilities":
                return {"operations": {op: {"contract_versions": [2], "implemented": True, "available": True} for op in OPS}}
            if path == "/auth/owner/begin":
                if state["owner"] != "fixture-owner":
                    raise mod.BrokerError("gateway_http_403", "not owner")
                return {"challenge_id": "challenge", "summary": {"owner": state["owner"]}, "expires_at": time.time() + 120}
            if path == "/auth/owner/confirm":
                if state["confirm_error"]:
                    raise mod.BrokerError(state["confirm_error"], "synthetic-password 123456 bos1.synthetic-owner-secret", 30)
                return {"owner_bearer": state["bearer"], "owner_session": session}
            if path == "/auth/owner/status":
                return {"owner_session": session}
            return {"received": kwargs.get("body"), "items": []}

    monkeypatch.setattr(mod, "LiveTransport", Transport)
    stream = io.BytesIO()
    instance = mod.Broker(Transport(), {"sub": "fixture-owner", "token_type": "portal"}, mod.JsonLineSink(stream))
    yield mod, instance, state, session, stream, Transport
    instance.shutdown()
    sys.modules.pop(spec.name, None)


def confirm(instance):
    return instance.op_owner_confirm({"contract_version": 2, "challenge_id": "challenge",
                                      "password": "synthetic-password", "totp": "123456"})


def test_no_bearer_or_factors_in_replies_hello_ledger_or_logs(setup, capsys, tmp_path):
    mod, broker, state, session, stream, _ = setup
    touched = []
    broker.ledger = SimpleNamespace(remember=lambda *a: touched.append(a))
    result = confirm(broker)
    assert result == {"owner_session": session}
    assert broker.transport._token != state["bearer"]
    broker.sink.reply({"value": result})
    broker.sink.reply({"value": broker.op_hello({})})
    # The generic output barrier scrubs even an accidental downstream echo.
    broker.sink.event("test", {"text": state["bearer"]})
    captured = capsys.readouterr()
    output = stream.getvalue().decode() + captured.out + captured.err
    for secret in (state["bearer"], "synthetic-password", "123456"):
        assert secret not in output
    assert touched == []
    assert not list(tmp_path.rglob("*.json"))


def test_new_broker_loses_session_and_link_nonce(setup):
    mod, broker, state, _, _, Transport = setup
    confirm(broker)
    new = mod.Broker(Transport(), {"sub": "fixture-owner", "token_type": "portal"}, mod.JsonLineSink(io.BytesIO()))
    try:
        assert new._owner_link_nonce != broker._owner_link_nonce
        assert new.op_owner_status({}) == {"owner_session": {"active": False}}
        with pytest.raises(mod.BrokerError, match="owner_confirmation_required"):
            new._owner_rest("POST", "/agent-services/grants", body={})
    finally:
        new.shutdown()


@pytest.mark.parametrize("operation", ["owner.lock", "owner.logout"])
def test_lock_logout_revoke_and_clear_memory(setup, operation):
    _, broker, state, _, _, _ = setup
    confirm(broker)
    broker.handle({"id": 1, "op": operation, "args": {}})
    assert broker._owner_transport is None
    assert broker.op_owner_status({}) == {"owner_session": {"active": False}}
    assert state["hits"][-1][2] == "/auth/owner/lock"


@pytest.mark.parametrize("code", ["gateway_http_401", "gateway_http_429", "transport_unavailable"])
def test_proof_once_no_resend_and_safe_retry_hint(setup, code):
    mod, broker, state, _, _, _ = setup
    state["confirm_error"] = code
    with pytest.raises(mod.BrokerError) as exc:
        confirm(broker)
    assert len([h for h in state["hits"] if h[2] == "/auth/owner/confirm"]) == 1
    frame = mod._error_frame(1, exc.value)
    assert frame["error"]["retry_after"] == 30
    assert "synthetic-password" not in json.dumps(frame)
    assert "bos1.synthetic-owner-secret" not in json.dumps(frame)
    assert broker._owner_transport is None


def test_owner_transport_bound_to_portal_and_nonce_absolute_expiry(setup):
    mod, broker, _, session, _, _ = setup
    confirm(broker)
    assert broker._owner_transport.headers == {"X-Bob-Broker-Link": broker._owner_link_nonce, "X-Bob-Portal-Credential": "portal-jti"}
    expiry = session["expires_at"]
    assert broker.op_owner_status({})["owner_session"]["expires_at"] == expiry
    broker._owner_session["expires_at"] = time.time() - 1
    with pytest.raises(mod.BrokerError, match="owner_confirmation_required"):
        broker._owner_rest("GET", "/agent-services/grants")
    assert broker._owner_transport is None


def test_nonowner_and_agent_never_get_eligible_projection(setup):
    _, broker, state, _, _, _ = setup
    assert broker.op_hello({})["identity"]["owner_controls_eligible"] is True
    broker._owner_eligible = None
    state["owner"] = "foreign-owner"
    assert broker.op_hello({})["identity"]["owner_controls_eligible"] is False
    broker.identity["token_type"] = "agent"
    assert broker.op_hello({})["identity"]["owner_controls_eligible"] is False


def test_exact_integer_owner_forwarding_and_per_operation_denial(setup):
    mod, broker, state, _, _, _ = setup
    confirm(broker)
    raw = {"contract_version": 2, "project_id": "project", "request_key": "same-key", "expected_revision": None, "call_limit": "9223372036854775807"}
    assert broker.op_budgets_set(raw)["received"] == raw
    broker._rest = lambda *a, **kw: {"operations": {"runs.start": {"contract_versions": [2]}}}
    count = len(state["hits"])
    with pytest.raises(mod.BrokerError, match="client_upgrade_required"):
        broker.op_budgets_set(raw)
    assert len(state["hits"]) == count
