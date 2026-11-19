"""Shipping snapshot and source share the same separate owner transport tests."""
import json
from types import SimpleNamespace

import pytest

from tests.test_execution_broker_v2 import broker


def owner_transport(module, instance, monkeypatch):
    from auth import create_portal_token
    calls, owner_calls = [], []
    canary = "bos1.synthetic-owner-secret-canary"
    session = {"active": True, "owner": instance.sub, "actor": instance.sub, "lane": "a"*64,
               "expires_at": module.time.time()+300, "allowed_operations": ["runs.resume", "grants.issue"], "credential_epoch": 1}
    class Portal:
        base = "http://127.0.0.1:1"
        timeout = 12
        _token = create_portal_token(instance.sub, {})
        def _bearer(self):
            return self._token
        def rest(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            if path == "/capabilities":
                return {"operations": {op: {"contract_versions": [2], "available": True} for op in instance._handlers}}
            if path == "/auth/owner/begin":
                return {"challenge_id": "challenge", "summary": {"task": "S1"}}
            if path == "/auth/owner/confirm":
                return {"owner_bearer": canary, "owner_session": session}
            return {"ordinary": True, "received": kwargs.get("body")}
    class Owner:
        def __init__(self, base, token, **kwargs):
            self._token = token
            assert base == Portal.base and token == canary
            assert not kwargs.get("token_file")
            assert kwargs["owner_headers"]["X-Bob-Broker-Link"] == instance._owner_link_nonce
            assert kwargs["owner_headers"]["X-Bob-Portal-Credential"]
        def rest(self, method, path, **kwargs):
            owner_calls.append((method, path, kwargs))
            if path == "/auth/owner/status":
                return {"owner_session": session}
            return {"received": kwargs.get("body")}
    instance.transport = Portal()
    instance.identity["token_type"] = "portal"
    monkeypatch.setattr(module, "LiveTransport", Owner)
    return calls, owner_calls, canary, session


def test_owner_transport_never_replaces_portal_and_restart_loses_bearer(broker, monkeypatch):
    module, instance, _ = broker
    calls, owner_calls, canary, session = owner_transport(module, instance, monkeypatch)
    portal = instance.transport
    args = {"contract_version": 2, "run_id": "run-ref", "request_key": "lost-reply", "expected_revision": 7}
    with pytest.raises(module.BrokerError, match="owner_confirmation_required"):
        instance.op_runs_resume(args)
    instance.op_owner_begin({"operation": "runs.resume", "resource": {"run_id": "run-ref"}})
    confirmed = instance.op_owner_confirm({"challenge_id": "challenge", "password": "password-canary", "totp": "123456"})
    assert confirmed == {"owner_session": session} and canary not in json.dumps(confirmed)
    assert module.scrub(canary) == "[redacted]"
    assert "password-canary" not in module._OWNER_SECRETS
    assert instance.transport is portal and instance.identity["token_type"] == "portal"
    instance.op_runs_resume(args)
    assert owner_calls[-1][1].endswith("/resume")
    assert not any(path.endswith("/resume") for _,path,_ in calls)
    hello = instance.op_hello({})
    assert hello["identity"]["sub"] == instance.sub and hello["owner_session"] == session
    assert canary not in json.dumps(hello)
    instance.op_grants_prepare({"request_key": "proposal", "request": {"task": "S1"}})
    assert calls[-1][1] == "/agent-services/grant-proposals"
    restarted = module.Broker(portal, instance.identity, SimpleNamespace())
    assert restarted._owner_link_nonce != instance._owner_link_nonce
    assert restarted.op_owner_status({}) == {"owner_session": {"active": False}}
    with pytest.raises(module.BrokerError, match="owner_confirmation_required"):
        restarted.op_runs_resume(args)
    assert instance.op_owner_lock({}) == {"owner_session": {"active": False}}
    assert canary not in module._OWNER_SECRETS and instance.transport is portal


def test_signed_in_confirmation_uses_separate_owner_transport_without_factors(broker, monkeypatch):
    module, instance, _ = broker
    calls, _, canary, session = owner_transport(module, instance, monkeypatch)
    args = {"challenge_id": "challenge", "confirmation_mode": "signed_in_user"}
    assert instance.op_owner_confirm(args) == {"owner_session": session}
    assert calls[-1][2]["body"] == args
    assert canary not in json.dumps(instance.op_owner_status({}))
    for extra in ({"password": "unused"}, {"totp": "123456"}, {"confirmation_mode": "unknown"}):
        with pytest.raises(module.BrokerError):
            instance.op_owner_confirm({**args, **extra})


def test_sensitive_confirm_failure_is_never_replayed_or_echoed(broker, monkeypatch):
    module, instance, _ = broker
    calls, _, _, _ = owner_transport(module, instance, monkeypatch)
    original = instance.transport.rest
    def failure(method, path, **kwargs):
        if path == "/auth/owner/confirm":
            calls.append((method,path,kwargs))
            raise module.BrokerError("transport_unavailable", "password-canary 123456")
        return original(method,path,**kwargs)
    instance.transport.rest = failure
    with pytest.raises(module.BrokerError) as error:
        instance.op_owner_confirm({"challenge_id": "challenge", "password": "password-canary", "totp": "123456"})
    assert "password-canary" not in error.value.detail and "123456" not in error.value.detail
    assert sum(path == "/auth/owner/confirm" for _,path,_ in calls) == 1
    assert instance._owner_transport is None


def test_owner_integer_strings_forward_exactly_and_unavailable_never_sends(broker, monkeypatch):
    module, instance, _ = broker
    _, owner_calls, _, _ = owner_transport(module, instance, monkeypatch)
    instance.op_owner_confirm({"challenge_id": "challenge", "password": "password-canary", "totp": "123456"})
    args = {"contract_version": 2, "project_id": "p", "request_key": "exact-key", "expected_revision": None,
            "call_limit": "9223372036854775807"}
    instance.op_budgets_set(args)
    assert owner_calls[-1][2]["body"] == args
    for bad in (9007199254740992, "01", "9223372036854775808", "+3", "3.0", True):
        with pytest.raises(module.BrokerError):
            instance.op_budgets_set({**args, "call_limit": bad})
    instance._rest = lambda *a,**kw: {"operations": {"grants.issue": {"contract_versions": [2], "available": False, "reason": "worker_boundary_required"}}}
    with pytest.raises(module.BrokerError, match="operation_unavailable"):
        instance.op_grants_issue({})
