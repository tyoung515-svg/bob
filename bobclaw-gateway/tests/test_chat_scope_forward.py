"""Neck Beard P3 — the gateway forwards the AGENT token's vouched scope to core.

The security claim: the forwarded scope is taken ONLY from the verified token claims
(never the client frame), it is attested with an HMAC the receiver (core) can validate,
and a human token forwards nothing (no regression to the human chat path).
"""
from routers.chat import _agent_scope_fields, _is_pin_authoritative
from core.permissions import verify_scope

SECRET = "shared-secret-please-at-least-32-chars!!"


def test_pin_authoritative_only_for_agent_token_with_a_face():
    # Headless contract: an agent token's explicit face is authoritative...
    assert _is_pin_authoritative({"token_type": "agent"}, "planner-cc-edit") is True
    # ...but only when a face is actually set,
    assert _is_pin_authoritative({"token_type": "agent"}, None) is False
    # and NEVER for a human/admin token (interactive heuristic unchanged).
    assert _is_pin_authoritative({"sub": "admin"}, "planner-cc-edit") is False
    assert _is_pin_authoritative(None, "planner-cc-edit") is False


def test_human_token_forwards_no_scope():
    # No token_type (human/admin) ⇒ {} even if a scope-looking claim is present.
    assert _agent_scope_fields({"sub": "admin"}, SECRET) == {}
    assert _agent_scope_fields(
        {"sub": "admin", "scope": {"auto_actions": ["cc_edit"]}}, SECRET
    ) == {}
    assert _agent_scope_fields(None, SECRET) == {}


def test_agent_token_forwards_a_valid_vouch():
    scope = {"auto_actions": ["cc_edit"], "may_touch": ["core/teams.py"]}
    out = _agent_scope_fields({"sub": "admin", "token_type": "agent", "scope": scope}, SECRET)
    assert out["scope"] == scope
    # The vouch validates against the SAME shared secret core will use.
    assert verify_scope(out["scope"], out["scope_vouch"], SECRET) is True


def test_agent_token_without_dict_scope_forwards_nothing():
    assert _agent_scope_fields({"token_type": "agent"}, SECRET) == {}
    assert _agent_scope_fields({"token_type": "agent", "scope": None}, SECRET) == {}
    assert _agent_scope_fields({"token_type": "agent", "scope": "not-a-dict"}, SECRET) == {}


def test_forwarded_vouch_is_rejected_under_a_different_secret():
    scope = {"auto_actions": ["cc_edit"]}
    out = _agent_scope_fields({"token_type": "agent", "scope": scope}, SECRET)
    assert verify_scope(
        out["scope"], out["scope_vouch"], "a-different-secret-32-chars-long!!"
    ) is False


def test_empty_gateway_secret_forwards_an_unusable_vouch():
    # If the gateway's BOBCLAW_SECRET is empty the vouch is "" → core fails closed.
    scope = {"auto_actions": ["cc_edit"]}
    out = _agent_scope_fields({"token_type": "agent", "scope": scope}, "")
    assert out["scope"] == scope
    assert out["scope_vouch"] == ""
    assert verify_scope(out["scope"], out["scope_vouch"], SECRET) is False
