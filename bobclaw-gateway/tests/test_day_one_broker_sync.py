"""Broker sync/transport regressions, without sockets or POSIX filesystem claims."""
import base64
from contextlib import nullcontext
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest


BACKEND = Path(__file__).resolve().parents[2]
BROKERS = [BACKEND / "portal-broker/portal-broker.py",
           BACKEND.parent / "app/portal/portal-broker.py"]


@pytest.fixture(autouse=True)
def _isolated_gateway_db():
    # These are standalone stdlib broker tests; no application DB is involved.
    yield


@pytest.fixture(params=BROKERS, ids=["backend", "app"])
def broker_path(request):
    if not request.param.exists():
        pytest.skip("Sibling app broker copy absent")
    return request.param


def load_broker(path, monkeypatch):
    spec = importlib.util.spec_from_file_location("_day_one_broker_sync", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    fcntl = ModuleType("fcntl")
    fcntl.LOCK_EX, fcntl.LOCK_SH, fcntl.LOCK_UN = 2, 1, 8

    def forbidden_lock(*args):
        raise AssertionError("This suite must not exercise POSIX file locking")

    fcntl.flock = forbidden_lock
    context = patch.dict(sys.modules, {"fcntl": fcntl}) if sys.platform == "win32" else nullcontext()
    with context:
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def broker_module(broker_path, monkeypatch):
    return load_broker(broker_path, monkeypatch)


def test_canonical_and_backend_brokers_are_byte_identical_lf():
    if not BROKERS[1].exists():
        pytest.skip("Sibling app broker copy absent")
    canonical = BROKERS[1].read_bytes()
    assert BROKERS[0].read_bytes() == canonical
    assert b"\r" not in canonical


@pytest.mark.parametrize("override", [None, "/tmp/lane-broker.token"])
def test_default_token_path_and_day_one_version(broker_path, monkeypatch, override):
    if override is None:
        monkeypatch.delenv("BOB_BROKER_TOKEN_FILE", raising=False)
    else:
        monkeypatch.setenv("BOB_BROKER_TOKEN_FILE", override)
    module = load_broker(broker_path, monkeypatch)
    assert module.DEFAULT_TOKEN_FILE == override
    assert module.VERSION == "1.3.0"


def token(**changes):
    claims = {"sub": "fixture-owner", "token_type": "portal", "service_epoch": 1, **changes}
    segment = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return "synthetic." + segment + ".signature"


def capture_requests(module, monkeypatch, kind):
    requests = []
    if kind == "rest":
        def open_request(request, **kwargs):
            requests.append(dict(request.header_items()))
            return io.BytesIO(b"{}")

        monkeypatch.setattr(module.urllib.request, "build_opener",
                            lambda *args: SimpleNamespace(open=open_request))
        send = lambda transport: transport.rest("GET", "/auth/status")
    else:
        def websocket(host, port, path, headers, **kwargs):
            requests.append(headers)
            return SimpleNamespace()

        monkeypatch.setattr(module, "RawWebSocket", websocket)
        send = lambda transport: transport.ws_connect("/ws/chat")
    return requests, send


@pytest.mark.parametrize("kind", ["rest", "websocket"])
def test_requests_reload_renewed_token(broker_module, monkeypatch, kind):
    module = broker_module
    old = token(jti="old")
    current = [old]
    # Stub only the POSIX token-file reader; exercise real renewal validation.
    monkeypatch.setattr(module, "load_token", lambda path: current[0])
    requests, send = capture_requests(module, monkeypatch, kind)
    transport = module.LiveTransport("http://127.0.0.1:7836", old, token_file="synthetic.token")
    try:
        send(transport)
        current[0] = token(jti="renewed")
        send(transport)
        assert [headers["Authorization"] for headers in requests] == ["Bearer " + old, "Bearer " + current[0]]
    finally:
        module.set_secret("")


@pytest.mark.parametrize("kind", ["rest", "websocket"])
@pytest.mark.parametrize("changes,code", [
    ({"sub": "another-owner"}, "identity_mismatch"),
    ({"token_type": "agent"}, "identity_mismatch"),
    ({"service_epoch": 2}, "credential_epoch_changed"),
])
def test_requests_reject_changed_identity_or_epoch_before_io(broker_module, monkeypatch, kind, changes, code):
    module = broker_module
    old = token()
    monkeypatch.setattr(module, "load_token", lambda path: token(**changes))
    requests, send = capture_requests(module, monkeypatch, kind)
    transport = module.LiveTransport("http://127.0.0.1:7836", old, token_file="synthetic.token")
    with pytest.raises(module.BrokerError, match=code):
        send(transport)
    assert requests == []
    assert transport._token == old


@pytest.mark.parametrize("outcome", ["eligible", "mismatch", "denied", "unavailable"])
def test_hello_probes_owner_eligibility_only_once_per_process(broker_module, outcome):
    module = broker_module
    calls = []

    def rest(method, path, **kwargs):
        calls.append((method, path, kwargs))
        if path == "/capabilities":
            return {"operations": {"owner.begin": {"contract_versions": [2], "available": True}}}
        assert path == "/auth/owner/begin"
        if outcome in {"denied", "unavailable"}:
            raise module.BrokerError("gateway_http_404" if outcome == "denied" else "transport_unavailable")
        return {"summary": {"owner": "fixture-owner" if outcome == "eligible" else "another-owner"}}

    broker = module.Broker(SimpleNamespace(rest=rest),
                           {"sub": "fixture-owner", "token_type": "portal"}, SimpleNamespace())
    try:
        for _ in range(3):
            hello = broker.op_hello({})
            assert hello["identity"]["owner_controls_eligible"] is (outcome == "eligible")
            assert hello["owner_session"] == {"active": False}
        probes = [call for call in calls if call[1] == "/auth/owner/begin"]
        assert len(probes) == 1
        assert probes[0][2]["body"] == {"contract_version": 2, "operation": "grants.list", "resource": {}}
        # The discovery cache never grants owner authority.
        with pytest.raises(module.BrokerError, match="owner_confirmation_required"):
            broker._owner_rest("POST", "/api/work/publish", body={})
        # Explicit owner actions still consult the gateway after a cached denial.
        begin = {"contract_version": 2, "operation": "grants.list", "resource": {}}
        if outcome in {"denied", "unavailable"}:
            with pytest.raises(module.BrokerError):
                broker.op_owner_begin(begin)
        else:
            assert broker.op_owner_begin(begin)["summary"]
        assert len([call for call in calls if call[1] == "/auth/owner/begin"]) == 2
    finally:
        broker.shutdown()


@pytest.mark.parametrize("token_type,available", [("agent", True), ("portal", False)])
def test_hello_does_not_probe_ineligible_token_or_unavailable_operation(broker_module, token_type, available):
    module = broker_module
    calls = []

    def rest(method, path, **kwargs):
        calls.append(path)
        assert path == "/capabilities"
        return {"operations": {"owner.begin": {"contract_versions": [2], "available": available}}}

    broker = module.Broker(SimpleNamespace(rest=rest),
                           {"sub": "fixture-owner", "token_type": token_type}, SimpleNamespace())
    try:
        assert broker.op_hello({})["identity"]["owner_controls_eligible"] is False
        assert calls == ["/capabilities"]
    finally:
        broker.shutdown()


def test_unconfigured_backend_broker_exits_with_configuration_reason(monkeypatch, capsys):
    monkeypatch.delenv("BOB_BROKER_TOKEN_FILE", raising=False)
    module = load_broker(BROKERS[0], monkeypatch)
    assert module.main([]) == 2
    assert "Set BOB_BROKER_TOKEN_FILE or pass --token-file" in capsys.readouterr().err
    assert module.build_parser().parse_args(["--token-file", "/tmp/explicit.token"]).token_file == "/tmp/explicit.token"
