"""BoBClaw Core — U3 action registry tests (SPEC-UI-OVERHAUL §3 / Decision D4).

Verifies the actions-as-data registry loads + validates fail-loud (unknown risk tier and
malformed binding rejected with specific errors), that the SPEC §3 seed set is present with the
correct D11 risk tiers and D12 undo guardrail, and that ``as_payload()`` emits the documented
schema. The cross-service "each id maps to a REAL gateway op" assertion lives in the gateway
suite (test_capabilities_actions), which can see both the registry and the gateway routes.
"""
import unittest

import pytest

from core.actions import (
    RISK_TIERS,
    SEED_ACTIONS,
    Action,
    ActionBinding,
    ActionRegistry,
    get_default_registry,
)

# The SPEC §3 seed set with the risk tier each action must carry (D11).
_EXPECTED_RISK = {
    "create_team": "reversible",
    "delete_team": "reversible",
    "pin_face": "reversible",
    "switch_profile": "reversible",
    "forget_fact": "gated",
    "new_conversation": "reversible",
    "approve": "gated",
    "deny": "gated",
}

_SCHEMA_KEYS = {
    "id",
    "title",
    "description_plain",
    "params_schema",
    "risk",
    "undo_hint",
    "page_scope",
    "binding",
}


class TestActionRegistryLoads(unittest.TestCase):
    def test_default_registry_loads_seed_set(self):
        reg = get_default_registry()
        self.assertEqual(len(reg), len(_EXPECTED_RISK))
        self.assertEqual({a.id for a in reg.list_actions()}, set(_EXPECTED_RISK))

    def test_seed_actions_have_expected_risk_tiers(self):
        reg = ActionRegistry()
        for action_id, expected in _EXPECTED_RISK.items():
            self.assertIn(action_id, reg)
            self.assertEqual(reg.get(action_id).risk, expected, action_id)

    def test_every_risk_tier_is_valid(self):
        for a in ActionRegistry().list_actions():
            self.assertIn(a.risk, RISK_TIERS, a.id)

    def test_reversible_actions_carry_undo_hint(self):
        for a in ActionRegistry().list_actions():
            if a.risk == "reversible":
                self.assertTrue(a.undo_hint and a.undo_hint.strip(),
                                f"{a.id} reversible but missing undo_hint")

    def test_gated_actions_are_not_auto(self):
        reg = ActionRegistry()
        for action_id in ("forget_fact", "approve", "deny"):
            self.assertFalse(reg.get(action_id).auto, action_id)
        for action_id in ("create_team", "pin_face", "new_conversation"):
            self.assertTrue(reg.get(action_id).auto, action_id)

    def test_list_actions_sorted_and_unique(self):
        ids = [a.id for a in ActionRegistry().list_actions()]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(ids), len(set(ids)))


class TestPayloadShape(unittest.TestCase):
    def test_as_payload_has_full_schema(self):
        for entry in get_default_registry().as_payload():
            self.assertEqual(set(entry) & _SCHEMA_KEYS, _SCHEMA_KEYS,
                             f"missing schema keys on {entry.get('id')}")
            # binding present + typed
            self.assertIn(entry["binding"]["kind"], ("rest", "ws"))
            self.assertIsInstance(entry["params_schema"], dict)
            self.assertIsInstance(entry["page_scope"], list)

    def test_as_payload_is_json_serializable(self):
        import json

        json.dumps(get_default_registry().as_payload())  # must not raise

    def test_bindings_are_well_formed(self):
        for a in ActionRegistry().list_actions():
            b = a.binding
            if b.kind == "rest":
                self.assertTrue(b.method and b.path, a.id)
                self.assertIsNone(b.ws_type, a.id)
            else:
                self.assertTrue(b.ws_type, a.id)
                self.assertIsNone(b.method, a.id)
                self.assertIsNone(b.path, a.id)


class TestValidationFailLoud(unittest.TestCase):
    _base = {
        "id": "x",
        "title": "X",
        "description_plain": "does x",
        "params_schema": {},
        "risk": "read",
        "page_scope": [],
        "binding": {"kind": "rest", "method": "GET", "path": "/x"},
    }

    def test_unknown_risk_tier_rejected_with_specific_error(self):
        with pytest.raises(ValueError, match="unknown risk tier"):
            Action.model_validate({**self._base, "risk": "danger"})

    def test_reversible_without_undo_hint_rejected(self):
        with pytest.raises(ValueError, match="undo_hint"):
            Action.model_validate({**self._base, "risk": "reversible"})

    def test_empty_id_rejected(self):
        with pytest.raises(ValueError):
            Action.model_validate({**self._base, "id": "  "})

    def test_unknown_binding_kind_rejected(self):
        with pytest.raises(ValueError, match="unknown binding kind"):
            ActionBinding.model_validate({"kind": "carrier-pigeon", "path": "/x", "method": "GET"})

    def test_rest_binding_requires_method_and_path(self):
        with pytest.raises(ValueError, match="rest binding requires"):
            ActionBinding.model_validate({"kind": "rest", "method": "GET"})

    def test_ws_binding_requires_ws_type(self):
        with pytest.raises(ValueError, match="ws binding requires"):
            ActionBinding.model_validate({"kind": "ws"})

    def test_rest_binding_rejects_ws_type(self):
        with pytest.raises(ValueError, match="must not set 'ws_type'"):
            ActionBinding.model_validate(
                {"kind": "rest", "method": "GET", "path": "/x", "ws_type": "oops"}
            )

    def test_duplicate_action_id_rejected(self):
        with pytest.raises(ValueError, match="duplicate action id"):
            ActionRegistry([dict(self._base), dict(self._base)])

    def test_seed_actions_constant_all_validate(self):
        # Every raw seed dict must validate (guards against a typo in SEED_ACTIONS data).
        for raw in SEED_ACTIONS:
            Action.model_validate(raw)


if __name__ == "__main__":
    unittest.main()
