"""
BoBClaw Core — Unit tests for the Gate Router decision node (P2).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.nodes.cc_edit import route_approval
from core.nodes.gate import GATE_RECONCILE_PROMPT, GateDecision, gate_decide
from core.permissions import Scope


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _scope(**kwargs) -> Scope:
    """Build a Scope from keyword args, using sensible defaults."""
    defaults = {
        "branch": "feat/test",
        "may_touch": ["src/**", "tests/**"],
        "may_not_touch": [".secrets/**", "main"],
        "auto_actions": ["read", "write_in_scope", "run_tests"],
        "escalate_actions": ["schema_change", "dep_install"],
    }
    defaults.update(kwargs)
    return Scope.model_validate(defaults)


# ─── gate_decide: deterministic routing ───────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_decide_auto_action_in_scope():
    decision = await gate_decide(
        "write_in_scope",
        {"file_paths": ["src/main.py"]},
        _scope(),
    )
    assert decision.destination == "auto"
    assert "action 'write_in_scope' -> auto" in decision.reasons


@pytest.mark.asyncio
async def test_gate_decide_escalate_action_goes_to_gate():
    # Empty-string backend disables the critic tier so the ambiguous result
    # surfaces as "gate".
    decision = await gate_decide(
        "schema_change",
        {"file_paths": ["src/old.py"]},
        _scope(),
        critic_backend="",
    )
    assert decision.destination == "gate"


@pytest.mark.asyncio
async def test_gate_decide_may_not_touch_overrides_auto():
    decision = await gate_decide(
        "read",
        {"file_paths": [".secrets/key.pem"]},
        _scope(),
    )
    assert decision.destination == "human"


@pytest.mark.asyncio
async def test_gate_decide_unknown_path_goes_to_gate():
    decision = await gate_decide(
        "read",
        {"file_paths": ["README.md"]},
        _scope(),
        critic_backend="",
    )
    assert decision.destination == "gate"


@pytest.mark.asyncio
async def test_gate_decide_missing_scope_fails_closed():
    decision = await gate_decide("read", {}, None)
    assert decision.destination == "human"


@pytest.mark.asyncio
async def test_gate_decide_floor_action_human():
    scope = _scope(auto_actions=["purchase"])
    decision = await gate_decide("purchase", {}, scope)
    assert decision.destination == "human"


# ─── gate_decide: critic middle tier ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_decide_critic_approve_converts_gate_to_auto():
    with patch("core.nodes.gate.run_critic", AsyncMock(return_value=("approve", ["in scope"]))):
        decision = await gate_decide(
            "schema_change",
            {"file_paths": ["src/schema.sql"]},
            _scope(),
            critic_backend="minimax",
        )
    assert decision.destination == "auto"
    assert decision.critic_verdict == "approve"


@pytest.mark.asyncio
async def test_gate_decide_critic_reject_converts_gate_to_human():
    with patch("core.nodes.gate.run_critic", AsyncMock(return_value=("reject", ["too risky"]))):
        decision = await gate_decide(
            "schema_change",
            {"file_paths": ["src/schema.sql"]},
            _scope(),
            critic_backend="minimax",
        )
    assert decision.destination == "human"
    assert decision.critic_verdict == "reject"


@pytest.mark.asyncio
async def test_gate_decide_critic_flag_converts_gate_to_human():
    with patch("core.nodes.gate.run_critic", AsyncMock(return_value=("flag", ["scope drift"]))):
        decision = await gate_decide(
            "schema_change",
            {"file_paths": ["src/schema.sql"]},
            _scope(),
            critic_backend="minimax",
        )
    assert decision.destination == "human"
    assert decision.critic_verdict == "flag"


@pytest.mark.asyncio
async def test_gate_decide_critic_failure_converts_gate_to_human():
    with patch("core.nodes.gate.run_critic", AsyncMock(return_value=("none", ["critic_unavailable: timeout"]))):
        decision = await gate_decide(
            "schema_change",
            {"file_paths": ["src/schema.sql"]},
            _scope(),
            critic_backend="minimax",
        )
    assert decision.destination == "human"
    assert decision.critic_verdict == "none"


@pytest.mark.asyncio
async def test_gate_decide_auto_action_skips_critic():
    with patch("core.nodes.gate.run_critic") as mock_critic:
        decision = await gate_decide(
            "read",
            {"file_paths": ["src/main.py"]},
            _scope(),
            critic_backend="minimax",
        )
    assert decision.destination == "auto"
    mock_critic.assert_not_called()


@pytest.mark.asyncio
async def test_gate_decide_scope_reaches_critic():
    """Regression: the critic prompt must include the job scope.

    This test must FAIL against the scope-blind round-1 implementation and
    PASS once GATE_RECONCILE_PROMPT is used with the serialized scope.
    """
    scope = _scope(may_not_touch=[".secrets/**"])
    captured = {}

    async def _capture_run_critic(*, subtask_text, worker_output, critic_backend, prompt_template):
        captured["subtask_text"] = subtask_text
        captured["worker_output"] = worker_output
        captured["prompt_template"] = prompt_template
        return ("approve", ["in scope"])

    with patch("core.nodes.gate.run_critic", side_effect=_capture_run_critic):
        await gate_decide(
            "schema_change",
            {"file_paths": ["src/schema.sql"]},
            scope,
            critic_backend="minimax",
        )

    # The serialized scope must appear in the critic's subtask_text.
    assert ".secrets/**" in captured["subtask_text"]
    assert "may_not_touch" in captured["subtask_text"]
    assert "schema_change" in captured["worker_output"]
    assert captured["prompt_template"] is GATE_RECONCILE_PROMPT


# ─── route_approval seam ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_route_approval_delegates_to_gate():
    details = {
        "scope": {
            "auto_actions": ["read"],
        },
    }
    assert await route_approval("read", details) == "auto"


@pytest.mark.asyncio
async def test_route_approval_no_scope_is_human():
    assert await route_approval("read", {}) == "human"


@pytest.mark.asyncio
async def test_route_approval_uses_critic_backend_from_details():
    details = {
        "scope": {
            "escalate_actions": ["schema_change"],
            "may_touch": ["src/**"],
        },
        "critic_backend": "minimax",
        "file_paths": ["src/schema.sql"],
    }
    with patch("core.nodes.gate.run_critic", AsyncMock(return_value=("approve", ["ok"]))):
        assert await route_approval("schema_change", details) == "auto"
