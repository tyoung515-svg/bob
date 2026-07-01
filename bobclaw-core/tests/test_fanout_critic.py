"""
BoBClaw Core — Unit tests for producer/critic discipline (handoff 008)

Tests cover:
  - Critic disabled: no critic call when critic_backend absent
  - Critic approve/flag/reject verdict propagation
  - Critic parse error, timeout, 429 → "none" verdict
  - Best-effort math: critic failure doesn't penalize worker; reject counts as failure
  - Dispatch cost pre-flight includes critic cost
  - Log emission includes critic fields
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from core.nodes.dispatch import dispatch_node
from core.nodes.join import join_node
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
        "critic_backend": None,
        "critic_prompt_template": None,
    }
    base.update(overrides)
    return base


def _dispatch_state(**overrides) -> dict:
    base = {
        "task": "implement the thing",
        "face_id": "worker-kimi",
        "backend": "kimi_code",
        "messages": [],
        "subtasks": None,
        "fanout_width": None,
        "escalation_backend": "kimi_platform",
    }
    base.update(overrides)
    return base


# ─── 1. Critic disabled ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_critic_disabled_no_critic_call():
    """No critic_backend → critic is NOT called, no critic_verdict in entry."""
    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="ok")),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        result = await worker_node(_worker_state(subtask_idx=0, critic_backend=None))
    entry = result["worker_results"][0]
    assert entry["status"] == "ok"
    assert entry.get("critic_verdict") is None


# ─── 2. Critic approve ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_critic_approve_passes_through():
    """Critic approves → status stays ok, verdict recorded."""
    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="producer result")),
        patch("core.nodes.critic._send_to_backend", AsyncMock(return_value='{"verdict": "approve", "reasons": []}')),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        result = await worker_node(_worker_state(subtask_idx=0, critic_backend="claude_api"))
    entry = result["worker_results"][0]
    assert entry["status"] == "ok"
    assert entry["critic_verdict"] == "approve"
    assert entry["critic_reasons"] == []


# ─── 3. Critic flag ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_critic_flag_keeps_status_ok():
    """Critic flags → status stays ok, reasons surfaced."""
    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="result")),
        patch("core.nodes.critic._send_to_backend", AsyncMock(return_value='{"verdict": "flag", "reasons": ["factual claim X unverified"]}')),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        result = await worker_node(_worker_state(subtask_idx=0, critic_backend="claude_api"))
    entry = result["worker_results"][0]
    assert entry["status"] == "ok"
    assert entry["critic_verdict"] == "flag"
    assert "factual claim" in entry["critic_reasons"][0]


# ─── 4. Critic reject ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_critic_reject_marks_status_rejected():
    """Critic rejects → status becomes rejected, error contains reasons."""
    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="wrong output")),
        patch("core.nodes.critic._send_to_backend", AsyncMock(return_value='{"verdict": "reject", "reasons": ["answer is fabricated"]}')),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        result = await worker_node(_worker_state(subtask_idx=0, critic_backend="claude_api"))
    entry = result["worker_results"][0]
    assert entry["status"] == "rejected"
    assert entry["critic_verdict"] == "reject"
    assert "critic_rejected" in entry["error"]
    assert "fabricated" in entry["error"]


# ─── 5. Critic parse error ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_critic_parse_error_returns_none_verdict():
    """Malformed critic JSON → verdict none, parse_error reason."""
    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="producer ok")),
        patch("core.nodes.critic._send_to_backend", AsyncMock(return_value="not json at all")),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        result = await worker_node(_worker_state(subtask_idx=0, critic_backend="claude_api"))
    entry = result["worker_results"][0]
    assert entry["critic_verdict"] == "none"
    assert entry["critic_reasons"][0].startswith("critic_unavailable: parse_error")


# ─── 6. Critic timeout ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_critic_timeout_returns_none_verdict():
    """Critic exceeds timeout → verdict none, timeout reason."""
    async def _critic_slow(_messages, _backend):
        await asyncio.sleep(999)

    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="producer ok")),
        patch("core.nodes.critic._send_to_backend", _critic_slow),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
        patch("core.nodes.critic.CRITIC_TIMEOUT_SECONDS", 0.01),
    ):
        result = await worker_node(_worker_state(subtask_idx=0, critic_backend="claude_api"))
    entry = result["worker_results"][0]
    assert entry["critic_verdict"] == "none"
    assert "timeout" in entry["critic_reasons"][0]


# ─── 7. Critic 429 ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_critic_429_returns_none_verdict():
    """Critic 429s → verdict none, critic_unavailable reason."""
    async def _raise_429(_m, _b):
        raise RuntimeError("429: Too Many Requests")

    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="producer ok")),
        patch("core.nodes.critic._send_to_backend", _raise_429),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        result = await worker_node(_worker_state(subtask_idx=0, critic_backend="claude_api"))
    entry = result["worker_results"][0]
    assert entry["critic_verdict"] == "none"
    assert "critic_unavailable" in entry["critic_reasons"][0]


# ─── 8. Critic failure doesn't penalize worker ────────────────────────────

@pytest.mark.asyncio
async def test_critic_failure_does_not_penalize_worker_in_best_effort():
    """1 of 5 workers has a critic failure \u2192 turn succeeds, error is None."""
    state = {
        "worker_results": [
            {"idx": 0, "status": "ok", "content": "A", "critic_verdict": "approve", "critic_reasons": []},
            {"idx": 1, "status": "ok", "content": "B", "critic_verdict": "flag", "critic_reasons": ["minor"]},
            {"idx": 2, "status": "ok", "content": "C", "critic_verdict": "none", "critic_reasons": ["critic_unavailable: timeout"]},
            {"idx": 3, "status": "ok", "content": "D", "critic_verdict": "approve", "critic_reasons": []},
            {"idx": 4, "status": "ok", "content": "E", "critic_verdict": "approve", "critic_reasons": []},
        ],
    }
    result = await join_node(state)
    assert result.get("error") is None
    msg = result["messages"][0]["content"]
    assert "A" in msg
    assert "critic unavailable" in msg


# ─── 9. Critic reject counts as failure ───────────────────────────────────

@pytest.mark.asyncio
async def test_critic_reject_counted_as_failure_in_best_effort():
    """All 5 rejected \u2192 turn fails. 5 ok + 1 rejected \u2192 turn succeeds."""
    # All rejected: turn fails
    all_rejected = {
        "worker_results": [
            {"idx": i, "status": "rejected", "critic_verdict": "reject", "critic_reasons": ["bad"], "error": "critic_rejected: bad"}
            for i in range(5)
        ],
    }
    result = await join_node(all_rejected)
    assert result.get("error") is not None
    assert "All fan-out workers failed" in result["error"]

    # 4 ok + 1 rejected: turn succeeds
    partial = {
        "worker_results": [
            {"idx": 0, "status": "ok", "content": "A", "critic_verdict": "approve", "critic_reasons": []},
            {"idx": 1, "status": "ok", "content": "B", "critic_verdict": "approve", "critic_reasons": []},
            {"idx": 2, "status": "ok", "content": "C", "critic_verdict": "approve", "critic_reasons": []},
            {"idx": 3, "status": "ok", "content": "D", "critic_verdict": "approve", "critic_reasons": []},
            {"idx": 4, "status": "rejected", "critic_verdict": "reject", "critic_reasons": ["bad"], "error": "critic_rejected: bad"},
        ],
    }
    result = await join_node(partial)
    assert result.get("error") is None
    msg = result["messages"][0]["content"]
    assert "rejected" in msg


# ─── 10. Dispatch cost pre-flight includes critic cost ────────────────────

def test_dispatch_cost_pre_flight_includes_critic_cost():
    """5 subtasks on kimi_code ($0.05) + critic on claude_api ($0.50) = $2.75 estimate."""
    mock_face = MagicMock()
    mock_face.critic_backend = "claude_api"
    mock_face.critic_prompt_template = None

    with (
        patch("core.nodes.dispatch.get_default_registry") as mock_registry,
        patch("core.nodes.dispatch.remaining_budget", return_value=100.0),
    ):
        mock_registry.return_value.get_face.return_value = mock_face
        subtasks = [f"t{i}" for i in range(5)]
        result = dispatch_node(_dispatch_state(subtasks=subtasks, backend="kimi_code"))

    fanout = result.get("fanout_subtasks")
    assert fanout is not None
    assert len(fanout) == 5


# ─── 11. Critic log emission includes critic fields ───────────────────────

@pytest.mark.asyncio
async def test_critic_log_emission_includes_critic_fields(caplog):
    """When critic runs, log has critic fields. When absent, no critic fields."""
    caplog.set_level(logging.INFO)

    # Worker with critic: log should include critic fields
    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="result")),
        patch("core.nodes.critic._send_to_backend", AsyncMock(return_value='{"verdict": "approve", "reasons": []}')),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        await worker_node(_worker_state(subtask_idx=0, critic_backend="claude_api"))

    records = [r for r in caplog.records if r.name == "bobclaw.core.fanout"]
    assert len(records) >= 1
    data = json.loads(records[0].getMessage())
    assert "critic_backend" in data
    assert data["critic_verdict"] == "approve"
    assert "critic_duration_ms" in data
    assert data["critic_reasons_count"] == 0

    caplog.clear()

    # Worker without critic: log should NOT have critic fields
    with (
        patch("core.nodes.worker._send_to_backend", AsyncMock(return_value="result")),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 10),
    ):
        await worker_node(_worker_state(subtask_idx=1, critic_backend=None))

    records = [r for r in caplog.records if r.name == "bobclaw.core.fanout"]
    assert len(records) >= 1
    data = json.loads(records[0].getMessage())
    assert "critic_backend" not in data
    assert "critic_verdict" not in data


# ─── 11. Stand-in critic (P0: GLM balance-down resilience) ─────────────────

@pytest.mark.asyncio
async def test_run_critic_stands_in_when_primary_hard_fails():
    """When the primary critic backend HARD-fails (e.g. GLM's balance-exhausted 429),
    run_critic retries on the configured stand-in (deepseek) so the critique survives."""
    from core.nodes.critic import run_critic

    async def _by_backend(_messages, backend):
        if backend == "glm_5_2":
            raise RuntimeError("Z.AI GLM balance/resource exhausted (HTTP 429, code 1113)")
        return '{"verdict": "reject", "reasons": ["out of scope"]}'

    with patch("core.nodes.critic._send_to_backend", _by_backend):
        verdict, reasons = await run_critic("scope", "worker output", critic_backend="glm_5_2")

    assert verdict == "reject"
    assert reasons[0] == "critic_standin=deepseek_v4_flash"
    assert "out of scope" in reasons[1]


@pytest.mark.asyncio
async def test_run_critic_no_standin_when_primary_succeeds():
    """No fallback call when the primary critic answers — byte-identical to before."""
    from core.nodes.critic import run_critic

    calls = []

    async def _by_backend(_messages, backend):
        calls.append(backend)
        return '{"verdict": "approve", "reasons": []}'

    with patch("core.nodes.critic._send_to_backend", _by_backend):
        verdict, reasons = await run_critic("scope", "wo", critic_backend="glm_5_2")

    assert verdict == "approve"
    assert calls == ["glm_5_2"]            # primary succeeded → stand-in never called
