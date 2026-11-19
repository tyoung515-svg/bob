"""Middleware refusals on the backend broker; no app copy, providers or sockets."""
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_day_one_broker_sync import load_broker


@pytest.fixture(autouse=True)
def _isolated_gateway_db():
    # These stdlib broker tests do not use the gateway database.
    yield


@pytest.fixture
def module(monkeypatch):
    return load_broker(Path(__file__).resolve().parents[2] / "portal-broker/portal-broker.py", monkeypatch)


def limited_opener(module, monkeypatch, body, *, status=429, headers=None):
    calls = []
    def open_request(request, **kwargs):
        calls.append(request)
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        raise module.urllib.error.HTTPError(request.full_url, status, "refused",
                                            headers or {}, io.BytesIO(raw))
    monkeypatch.setattr(module.urllib.request, "build_opener",
                        lambda *args: SimpleNamespace(open=open_request))
    return calls


def refusal(seconds=7):
    return {"error": "rate limit exceeded", "code": "rate_limited", "retry_after_seconds": seconds}


def assert_refusal(module, payload, seconds=7):
    assert payload["code"] == "rate_limited"
    assert payload["applied"] is False
    assert payload["message"] == "Bob is handling a lot of requests right now, so this wasn't sent."
    if seconds is None:
        assert "retry_after_seconds" not in payload
    else:
        assert payload["retry_after_seconds"] == seconds
    assert "unknown" not in json.dumps(payload).lower()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
def test_transport_marks_only_middleware_refusal_and_never_retries(module, monkeypatch, method):
    calls = limited_opener(module, monkeypatch, refusal(), headers={"Retry-After": "7"})
    transport = module.LiveTransport("http://127.0.0.1:1", "synthetic")
    with pytest.raises(module.BrokerError) as error:
        transport.rest(method, "/conversations", body={} if method != "GET" else None)
    assert_refusal(module, module._error_frame(1, error.value)["error"])
    assert len(calls) == 1


@pytest.mark.parametrize("seconds,header,expected", [
    (3, None, 3), (None, "5", 5), (None, None, None),
    (True, "4", 4), (-1, None, None), ("9", None, None),
])
def test_retry_metadata_is_optional_and_validated(module, monkeypatch, seconds, header, expected):
    limited_opener(module, monkeypatch, refusal(seconds), headers={"Retry-After": header} if header else {})
    with pytest.raises(module.BrokerError) as error:
        module.LiveTransport("http://127.0.0.1:1", "synthetic").rest("GET", "/faces")
    assert_refusal(module, module._error_frame(1, error.value)["error"], expected)


@pytest.mark.parametrize("status,body", [
    (429, {"error": "rate limit exceeded"}),
    (429, {"error": {"code": "rate_limited", "message": "provider quota"}}),
    (429, {"code": "rate_limited", "error": "provider quota"}),
    (429, {"provider": {"code": "rate_limited", "error": "rate limit exceeded"}}),
    (429, b"not JSON"), (429, b"\xff"), (429, b"x" * 4097),
    (503, refusal()),
])
def test_other_http_errors_keep_existing_handling(module, monkeypatch, status, body):
    calls = limited_opener(module, monkeypatch, body, status=status, headers={"Retry-After": "6"})
    with pytest.raises(module.BrokerError) as error:
        module.LiveTransport("http://127.0.0.1:1", "synthetic").rest("POST", "/settings/password", body={})
    payload = module._error_frame(1, error.value)["error"]
    assert payload["code"] == "gateway_http_%d" % status
    assert payload["retry_after"] == 6
    assert "applied" not in payload and "retry_after_seconds" not in payload
    assert len(calls) == 1


@pytest.mark.parametrize("op,args", [
    ("conversations.list", {}), ("conversations.create", {"title": "new"}),
    ("catalog", {}), ("hello", {}),
    ("runs.get", {"contract_version": 2, "run_id": "run-1"}),
    ("jobs.audit", {"contract_version": 2, "job_id": "a" * 32}),
])
def test_rpc_and_client_driven_polls_report_definite_refusal(module, monkeypatch, op, args):
    calls = limited_opener(module, monkeypatch, refusal())
    transport = module.LiveTransport("http://127.0.0.1:1", "synthetic")
    output = io.BytesIO()
    sink = module.JsonLineSink(output)
    broker = module.Broker(transport, {"sub": "owner"}, sink)
    module.serve(broker, sink, io.BytesIO(json.dumps({"id": 1, "op": op, "args": args}).encode() + b"\n"))
    frame = json.loads(output.getvalue())
    assert frame["ok"] is False and frame["id"] == 1
    assert_refusal(module, frame["error"])
    assert len(calls) == 1


def test_chat_preflight_refusal_sends_no_message(module, monkeypatch):
    calls = limited_opener(module, monkeypatch, refusal())
    transport = module.LiveTransport("http://127.0.0.1:1", "synthetic")
    opened = []
    monkeypatch.setattr(transport, "ws_connect", lambda *a, **kw: opened.append(a))
    events = []
    broker = module.Broker(transport, {"sub": "owner"}, SimpleNamespace(event=lambda name, data: events.append((name, data))))
    broker.op_chat_send({"conversation_id": "conversation-1", "content": "hello"})
    assert len(calls) == 1 and not opened
    errors = [data for name, data in events if name == "error"]
    assert len(errors) == 1
    assert_refusal(module, errors[0])
    assert any(name == "stopped" and data["code"] == "rate_limited" for name, data in events)


@pytest.mark.parametrize("op,args", [
    ("owner.begin", {"operation": "grants.list", "resource": {}}),
    ("owner.confirm", {"challenge_id": "challenge", "password": "password-canary", "totp": "123456"}),
    ("owner.status", {}), ("owner.lock", {}),
    ("secrets.set", {"contract_version": 2, "provider": "glm", "key": "key-canary", "request_key": "secret-key"}),
    ("auth.change_password", {"contract_version": 2, "current": "old-canary", "new": "new-canary", "totp": "123456", "request_key": "password-key"}),
    ("auth.factors.binding", {"contract_version": 2}),
    ("auth.factors.cancel", {"contract_version": 2, "challenge_id": "challenge", "request_key": "factor-key"}),
    ("policy.set", {"contract_version": 2, "policy": {}, "expected_revision": 1, "request_key": "policy-key"}),
])
def test_owner_remapping_preserves_refusal_without_retry(module, monkeypatch, op, args):
    calls = limited_opener(module, monkeypatch, refusal())
    transport = module.LiveTransport("http://127.0.0.1:1", "synthetic")
    broker = module.Broker(transport, {"sub": "owner", "token_type": "portal"}, SimpleNamespace())
    broker._owner_transport = transport
    broker._owner_session = {"active": True, "expires_at": module.time.time() + 300}
    monkeypatch.setattr(broker, "_require_owner_operation", lambda *a: {})
    with pytest.raises(module.BrokerError) as error:
        broker.handle({"id": 1, "op": op, "args": args})
    assert_refusal(module, module._error_frame(1, error.value)["error"])
    assert len(calls) == 1
    assert "canary" not in json.dumps(module._error_frame(1, error.value))


def test_throttled_project_detail_stops_fanout(module):
    calls = []
    def rest(method, path, **kwargs):
        calls.append(path)
        if path == "/projects":
            return {"items": [{"id": "p1"}, {"id": "p2"}]}
        raise module.GatewayRateLimited(retry_after=7)
    broker = module.Broker(SimpleNamespace(rest=rest), {"sub": "owner"}, SimpleNamespace())
    with pytest.raises(module.BrokerError) as error:
        broker.op_projects_list({})
    assert_refusal(module, module._error_frame(1, error.value)["error"])
    assert calls == ["/projects", "/projects/p1"]


def test_throttled_approval_readback_preserves_recorded_decision(module, monkeypatch):
    calls = []
    def rest(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            return {"decision": "recorded", "agent_resume": "requested"}
        raise module.GatewayRateLimited(retry_after=7)
    broker = module.Broker(SimpleNamespace(rest=rest), {"sub": "owner"}, SimpleNamespace(), allow_approvals=True)
    monkeypatch.setattr(broker, "_approval_row", lambda ident: {"id": ident, "status": "pending"})
    result = broker.op_approvals_decide({"approval_id": "a1", "decision": "approve"})
    assert result["decision"] == "recorded" and result["readback_failed"] is True
    assert_refusal(module, result["readback_refusal"])
    assert calls == [("POST", "/approvals/a1/decide"), ("GET", "/approvals/a1")]


def test_provider_code_alone_does_not_claim_non_application(module):
    error = module.BrokerError("rate_limited", "provider quota exhausted", retry_after=9)
    payload = module._error_frame(1, error)["error"]
    assert payload["detail"] == "provider quota exhausted"
    assert payload["retry_after"] == 9
    assert "applied" not in payload and "retry_after_seconds" not in payload


def test_throttled_followup_readback_does_not_claim_message_was_refused(module):
    calls = []
    def rest(method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            return {"reply": "manager response"}
        raise module.GatewayRateLimited(retry_after=7)
    broker = module.Broker(SimpleNamespace(rest=rest), {"sub": "owner"}, SimpleNamespace())
    result = broker.op_runs_followup({"run_id": "r1", "content": "follow up"})
    assert result["manager_reply"] == {"reply": "manager response"}
    assert result["run"] is None and result["readback_failed"] is True
    assert_refusal(module, result["readback_refusal"])
    assert calls == [("POST", "/agent-services/runs/r1/messages"), ("GET", "/agent-services/runs/r1")]


@pytest.mark.parametrize("marked", [False, True])
def test_startup_errors_preserve_refusal_metadata_and_existing_other_shape(module, monkeypatch, capsys, marked):
    monkeypatch.setattr(module, "load_token", lambda path: "synthetic")
    monkeypatch.setattr(module, "decode_claims", lambda token: {})
    def bind(*args):
        if marked:
            raise module.GatewayRateLimited(retry_after=7)
        raise module.BrokerError("token_rejected")
    monkeypatch.setattr(module, "bind_identity", bind)
    assert module.main(["--token-file", "synthetic-token-file"]) == 3
    failure = json.loads(capsys.readouterr().err)
    if marked:
        assert_refusal(module, failure["error"])
    else:
        assert failure == {"ok": False, "error": {"code": "token_rejected", "detail": ""}}
