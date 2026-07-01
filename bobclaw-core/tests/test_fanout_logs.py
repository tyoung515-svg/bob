"""
BoBClaw Core — Unit tests for per-worker structured logging (handoff 007)

Tests cover:
  - bobclaw.core.fanout log emitted per worker completion
  - No payload leakage (task text or response content) in log records
"""
from __future__ import annotations

import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from core.nodes.worker import worker_node


def _worker_state(**overrides) -> dict:
    base = {
        "task": "subtask a",
        "backend": "kimi_code",
        "face_id": "worker-kimi",
        "escalation_backend": "kimi_platform",
        "subtask_idx": 0,
        "messages": [],
        "phase": "dispatch",
    }
    base.update(overrides)
    return base


def _mock_send(response: str = "ok result"):
    return AsyncMock(return_value=response)


@pytest.mark.asyncio
async def test_fanout_log_emitted_per_worker(caplog):
    """3 workers each produce one bobclaw.core.fanout log record."""
    caplog.set_level(logging.INFO)
    with (
        patch("core.nodes.worker._send_to_backend", _mock_send("done")),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        await worker_node(_worker_state(subtask_idx=0, task="task one"))
        await worker_node(_worker_state(subtask_idx=1, task="task two"))
        await worker_node(_worker_state(subtask_idx=2, task="task three"))

    records = [r for r in caplog.records if r.name == "bobclaw.core.fanout"]
    assert len(records) == 3

    expected_keys = {"ts", "turn_id", "worker_idx", "status", "duration_ms", "backend", "backend_used", "usage"}
    for record in records:
        data = json.loads(record.getMessage())
        assert set(data.keys()) == expected_keys, f"Got keys: {set(data.keys())}"


@pytest.mark.asyncio
async def test_fanout_log_no_payload_leak(caplog):
    """Log records must not contain task text or response content."""
    caplog.set_level(logging.INFO)
    task_text = "super-secret-task-content"
    response_text = "super-secret-response-content"
    with (
        patch("core.nodes.worker._send_to_backend", _mock_send(response_text)),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        await worker_node(_worker_state(subtask_idx=0, task=task_text))

    records = [r for r in caplog.records if r.name == "bobclaw.core.fanout"]
    assert len(records) == 1
    log_msg = records[0].getMessage()
    assert task_text not in log_msg
    assert response_text not in log_msg
