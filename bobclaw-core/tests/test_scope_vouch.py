"""Neck Beard P3 — gateway→core scope vouch (core/permissions.py).

The security claim lives here: the vouch is bound to the EXACT canonical scope, so a
captured vouch cannot be replayed against a wider scope; it fails closed on an empty
secret/vouch; and the canonicalization is independent of dict key order / extra keys so
the gateway and core agree byte-for-byte.
"""
from core.permissions import (
    Scope,
    canonical_scope_json,
    compute_scope_vouch,
    scope_vouch,
    verify_scope,
    verify_scope_vouch,
)

SECRET = "shared-secret-please-at-least-32-chars!!"


# ── string-level primitives (the build-pipe-built core) ──────────────────────

def test_compute_empty_secret_returns_empty():
    assert compute_scope_vouch("anything", "") == ""


def test_verify_fails_closed_on_empty_secret_or_vouch():
    v = compute_scope_vouch("canon", SECRET)
    assert verify_scope_vouch("canon", v, "") is False
    assert verify_scope_vouch("canon", "", SECRET) is False
    assert verify_scope_vouch("canon", v, SECRET) is True


def test_roundtrip_and_tamper_detection():
    v = compute_scope_vouch("canonical-x", SECRET)
    assert verify_scope_vouch("canonical-x", v, SECRET) is True
    assert verify_scope_vouch("canonical-x", v + "0", SECRET) is False   # tampered vouch
    assert verify_scope_vouch("canonical-y", v, SECRET) is False         # different canonical
    assert verify_scope_vouch("canonical-x", v, SECRET + "x") is False   # different secret


# ── canonicalization (key-order / extra-key independence) ────────────────────

def test_canonical_is_key_order_independent():
    a = {"may_touch": ["core/teams.py"], "auto_actions": ["cc_edit"]}
    b = {"auto_actions": ["cc_edit"], "may_touch": ["core/teams.py"]}
    assert canonical_scope_json(a) == canonical_scope_json(b)


def test_canonical_strips_unknown_keys():
    # Scope ignores extras (extra='ignore') — so junk keys don't change the canonical
    # form, which is what makes the vouch attest to the VALIDATED scope only.
    base = {"may_touch": ["x"]}
    junk = {"may_touch": ["x"], "bogus": 123, "zzz": [1, 2]}
    assert canonical_scope_json(base) == canonical_scope_json(junk)


def test_canonical_accepts_scope_instance():
    s = Scope(auto_actions=["cc_edit"])
    assert canonical_scope_json(s) == canonical_scope_json({"auto_actions": ["cc_edit"]})


# ── Scope-aware wrappers (the security boundary) ─────────────────────────────

def test_scope_vouch_roundtrip_via_wrappers():
    s = {"auto_actions": ["cc_edit"], "may_touch": ["core/teams.py"]}
    v = scope_vouch(s, SECRET)
    assert v  # non-empty
    assert verify_scope(s, v, SECRET) is True
    # a key-order-different dict still verifies (canonicalization)
    assert verify_scope({"may_touch": ["core/teams.py"], "auto_actions": ["cc_edit"]},
                        v, SECRET) is True


def test_scope_widening_attack_is_blocked():
    narrow = {"auto_actions": [], "may_touch": ["core/teams.py"]}
    v = scope_vouch(narrow, SECRET)
    widened = {"auto_actions": ["cc_edit", "merge_to_main"], "may_touch": ["**"]}
    # The vouch is bound to the NARROW scope; the widened scope cannot reuse it.
    assert verify_scope(widened, v, SECRET) is False


def test_scope_vouch_empty_secret_grants_nothing():
    s = {"auto_actions": ["cc_edit"]}
    assert scope_vouch(s, "") == ""
    assert verify_scope(s, "anything", "") is False


def test_non_string_vouch_fails_closed_without_raising():
    # vouch is the attacker-controlled wire field — a non-str must return False, never
    # raise TypeError out of hmac.compare_digest (regression: review finding #1).
    s = {"auto_actions": ["cc_edit"]}
    for bad in (["x"], {"a": 1}, 123, 3.5, True):
        assert verify_scope(s, bad, SECRET) is False
        assert verify_scope_vouch("canon", bad, SECRET) is False
    # non-str canonical / secret likewise total (no raise)
    assert verify_scope_vouch(123, "v", SECRET) is False
    assert compute_scope_vouch(123, SECRET) == ""
    assert compute_scope_vouch("c", 123) == ""


def test_malformed_scope_fails_closed():
    bad_type = {"budget_usd": "not-a-number"}   # fails Scope validation
    not_a_dict = ["auto_actions"]               # not even a scope object
    assert scope_vouch(bad_type, SECRET) == ""
    assert scope_vouch(not_a_dict, SECRET) == ""
    assert verify_scope(bad_type, "x", SECRET) is False
    assert verify_scope(not_a_dict, "x", SECRET) is False
