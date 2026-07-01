"""
BoBClaw Core — Unit tests for fan-out cost-cap pre-flight (handoff 007)

Tests cover:
  - Cost pre-flight aborts when estimate exceeds remaining budget
  - Cost pre-flight passes when estimate is under remaining budget
  - Unmapped backend raises a config error
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from core.nodes.dispatch import dispatch_node


def _state(**overrides) -> dict:
    base = {
        "task": "implement the thing",
        "face_id": "worker-kimi",
        "backend": "kimi_platform",
        "messages": [],
        "subtasks": None,
        "fanout_width": None,
        "escalation_backend": "kimi_platform",
    }
    base.update(overrides)
    return base


def test_cost_pre_flight_aborts_when_estimate_exceeds_budget():
    """5 workers * $0.10 > $0.01 remaining → dispatch returns error."""
    subtasks = ["a", "b", "c", "d", "e"]
    with patch("core.nodes.dispatch.remaining_budget", return_value=0.01):
        result = dispatch_node(_state(subtasks=subtasks, backend="kimi_platform"))
    assert "error" in result
    assert "cost-cap" in result["error"].lower()
    assert "$0.50" in result["error"]  # 5 * $0.10
    assert result.get("fanout_subtasks") is None


def test_cost_pre_flight_passes_when_estimate_under_budget():
    """5 workers * $0.10 < $20.00 (default) → dispatch fans out normally."""
    subtasks = ["a", "b", "c", "d", "e"]
    result = dispatch_node(_state(subtasks=subtasks, backend="kimi_platform"))
    fanout = result.get("fanout_subtasks")
    assert fanout is not None
    assert len(fanout) == 5


def test_unmapped_backend_raises_config_error():
    """A backend not in the fan-out dicts raises ValueError."""
    subtasks = ["a", "b", "c", "d", "e"]
    with pytest.raises(ValueError, match="no MAX_FANOUT_WIDTH_BY_BACKEND entry"):
        dispatch_node(_state(subtasks=subtasks, backend="nonexistent_backend"))
