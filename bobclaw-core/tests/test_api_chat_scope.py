"""Neck Beard P3 — core /api/chat scope ingress (api/server.py._resolve_ingress_scope).

Negative-first: the security claim is that a scope is honored ONLY with a valid gateway
vouch, so a direct-to-core self-grant is stripped to None (destructive → human). The
{} / None / malformed cases keep distinct semantics.
"""
from api.server import _resolve_ingress_scope
from core.permissions import scope_vouch

SECRET = "shared-secret-please-at-least-32-chars!!"


def test_absent_scope_returns_none():
    # Legacy turn (human chat, no scope) — unchanged.
    assert _resolve_ingress_scope(None, None, SECRET) is None
    assert _resolve_ingress_scope(None, "ignored", SECRET) is None


def test_valid_vouched_scope_is_honored():
    s = {"auto_actions": ["cc_edit"], "may_touch": ["core/teams.py"]}
    v = scope_vouch(s, SECRET)
    assert _resolve_ingress_scope(s, v, SECRET) == s


def test_empty_vouched_scope_distinct_from_none():
    # {} (vouched) → all-gate (kept, not collapsed to None/legacy).
    v = scope_vouch({}, SECRET)
    assert _resolve_ingress_scope({}, v, SECRET) == {}


def test_unvouched_scope_is_stripped_self_grant_blocked():
    # The attack: a caller asserts a fat scope with no/forged vouch → stripped to None.
    fat = {"auto_actions": ["cc_edit", "merge_to_main"], "may_touch": ["**"]}
    assert _resolve_ingress_scope(fat, None, SECRET) is None
    assert _resolve_ingress_scope(fat, "forged-vouch", SECRET) is None


def test_wrong_secret_vouch_is_stripped():
    s = {"auto_actions": ["cc_edit"]}
    v = scope_vouch(s, "attacker-secret-not-the-real-one-aaaa!!")
    assert _resolve_ingress_scope(s, v, SECRET) is None


def test_malformed_scope_is_stripped():
    # Present but invalid → no vouch can match → stripped.
    assert _resolve_ingress_scope({"budget_usd": "abc"}, "x", SECRET) is None


def test_non_string_vouch_is_stripped_not_500():
    # A crafted payload with a non-str scope_vouch must strip to None (fail closed),
    # never raise out of the /api/chat handler (regression: review finding #1).
    s = {"auto_actions": ["cc_edit"]}
    assert _resolve_ingress_scope(s, ["x"], SECRET) is None
    assert _resolve_ingress_scope(s, {"a": 1}, SECRET) is None
    assert _resolve_ingress_scope(s, 123, SECRET) is None


def test_empty_server_secret_strips_even_a_real_vouch():
    # If core's BOBCLAW_SECRET is empty, NOTHING is honored (fail closed).
    s = {"auto_actions": ["cc_edit"]}
    v = scope_vouch(s, SECRET)
    assert _resolve_ingress_scope(s, v, "") is None
