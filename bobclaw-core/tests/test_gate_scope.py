"""
BoBClaw Core — Unit tests for the Gate Router deterministic policy layer.
"""
from __future__ import annotations

import pytest

from core.nodes.cc_edit import route_approval
from core.permissions import Scope, evaluate_action, evaluate_path


# ─── Scope parsing ────────────────────────────────────────────────────────────

def test_scope_parses_canonical_dict():
    raw = {
        "branch": "feat/gate-router-p1",
        "may_touch": ["bobclaw-core/core/permissions.py", "tests/**"],
        "may_not_touch": [".secrets/**", "main"],
        "auto_actions": ["read", "write_in_scope", "run_tests"],
        "escalate_actions": ["file_delete", "schema_change"],
        "budget_usd": 5.0,
    }
    scope = Scope.model_validate(raw)
    assert scope.branch == "feat/gate-router-p1"
    assert "tests/**" in scope.may_touch
    assert ".secrets/**" in scope.may_not_touch
    assert "run_tests" in scope.auto_actions
    assert "file_delete" in scope.escalate_actions
    assert scope.budget_usd == 5.0


def test_scope_uses_sensible_defaults():
    scope = Scope.model_validate({})
    assert scope.branch is None
    assert scope.may_touch == []
    assert scope.may_not_touch == []
    assert scope.auto_actions == []
    assert scope.escalate_actions == []
    assert scope.budget_usd is None


# ─── evaluate_action ──────────────────────────────────────────────────────────

def test_action_missing_scope_is_human():
    assert evaluate_action("read", None) == "human"


def test_empty_action_is_human():
    scope = Scope(auto_actions=["read"])
    assert evaluate_action("", scope) == "human"


@pytest.mark.parametrize("action", [
    "email_send",
    "email_reply",
    "form_submit",
    "purchase",
    "file_delete",
    "shell_dangerous",
    "merge_to_main",
])
def test_always_human_floor_overrides_scope(action):
    # The IRREVERSIBLE / OUTWARD floor: never auto-cleared, even when a scope lists it.
    scope = Scope(auto_actions=[action], escalate_actions=[action])
    assert evaluate_action(action, scope) == "human"


def test_cc_edit_is_gateable_not_always_human():
    # Neck Beard P3: cc_edit dropped OUT of the always-human floor so a scoped agent can
    # auto-clear an in-scope edit. No scope → human; an empty scope → "gate" (the
    # unknown-action fail-closed: needs a critic, surfaces to human without one — NOT a
    # silent auto); listed in auto_actions → auto; merge_to_main stays human even when
    # listed (the irreversible floor is intact).
    assert evaluate_action("cc_edit", None) == "human"            # no scope → human
    assert evaluate_action("cc_edit", Scope()) == "gate"           # empty scope → gate (→human w/o critic)
    assert evaluate_action("cc_edit", Scope(auto_actions=["cc_edit"])) == "auto"
    assert evaluate_action("merge_to_main",
                           Scope(auto_actions=["merge_to_main"])) == "human"


def test_auto_action_routes_to_auto():
    scope = Scope(auto_actions=["read", "write_in_scope"])
    assert evaluate_action("read", scope) == "auto"
    assert evaluate_action("write_in_scope", scope) == "auto"


def test_escalate_action_routes_to_gate():
    scope = Scope(escalate_actions=["schema_change", "dep_install"])
    assert evaluate_action("schema_change", scope) == "gate"
    assert evaluate_action("dep_install", scope) == "gate"


def test_unknown_action_fails_closed_to_gate():
    scope = Scope(auto_actions=["read"])
    assert evaluate_action("deploy_to_production", scope) == "gate"


# ─── evaluate_path ────────────────────────────────────────────────────────────

def test_path_missing_scope_is_gate():
    assert evaluate_path("src/main.py", None) == "gate"


def test_empty_path_is_gate():
    scope = Scope(may_touch=["src/**"])
    assert evaluate_path("", scope) == "gate"


def test_may_not_touch_routes_to_human():
    scope = Scope(may_not_touch=[".secrets/**", "main"])
    assert evaluate_path(".secrets/bobclaw.env", scope) == "human"
    assert evaluate_path("main", scope) == "human"


def test_may_touch_routes_to_auto():
    scope = Scope(may_touch=["bobclaw-core/**/*.py", "tests/**"])
    assert evaluate_path("bobclaw-core/core/permissions.py", scope) == "auto"
    assert evaluate_path("tests/test_gate_scope.py", scope) == "auto"


def test_uncovered_path_fails_closed_to_gate():
    scope = Scope(may_touch=["bobclaw-core/**"])
    assert evaluate_path("README.md", scope) == "gate"


def test_may_not_touch_wins_over_may_touch():
    scope = Scope(
        may_touch=["**/*.py"],
        may_not_touch=[".secrets/**"],
    )
    assert evaluate_path(".secrets/config.py", scope) == "human"


# ─── route_approval seam ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_approval_without_scope_stays_human():
    assert await route_approval("cc_edit", {"file_path": "foo.py"}) == "human"


@pytest.mark.asyncio
async def test_route_approval_with_scope_uses_policy():
    details = {
        "scope": {
            "auto_actions": ["write_in_scope", "run_tests"],
        },
    }
    assert await route_approval("write_in_scope", details) == "auto"


@pytest.mark.asyncio
async def test_route_approval_floor_still_human_with_scope():
    details = {
        "scope": {
            "auto_actions": ["merge_to_main"],
        },
    }
    assert await route_approval("merge_to_main", details) == "human"


@pytest.mark.asyncio
async def test_route_approval_malformed_scope_fails_closed_to_human():
    details = {
        "scope": {
            "auto_actions": "not-a-list",  # invalid
        },
    }
    assert await route_approval("read", details) == "human"
