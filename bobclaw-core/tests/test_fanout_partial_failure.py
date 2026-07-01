"""
BoBClaw Core — Unit tests for fan-out partial-failure semantics (handoff 006)

Tests cover:
  - Best-effort: ≥1 worker success ⇒ turn succeeds
  - All workers fail ⇒ state["error"] is set
  - Per-worker timeout via asyncio.wait_for
  - 429 detection in worker_node
  - 429 mid-fan-out: in-flight workers continue
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.nodes.join import join_node
from core.nodes.worker import worker_node


# ─── Helpers ─────────────────────────────────────────────────────────────

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
    """Return an AsyncMock for _send_to_backend that returns *response*."""
    return AsyncMock(return_value=response)


def _mock_send_slow(delay: float = 999):
    """Return an AsyncMock that sleeps for *delay* seconds."""
    async def slow(_messages, _backend):
        await asyncio.sleep(delay)
        return "too late"
    return slow


# ─── Best-effort join tests ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_partial_failure_one_worker_fails_turn_succeeds():
    """Best-effort: 1 of 5 fails, state.error is None, message names failure."""
    state = {
        "worker_results": [
            {"idx": 0, "text": "a", "status": "ok", "content": "A ok"},
            {"idx": 1, "text": "b", "status": "failed", "error": "timeout"},
            {"idx": 2, "text": "c", "status": "ok", "content": "C ok"},
            {"idx": 3, "text": "d", "status": "ok", "content": "D ok"},
            {"idx": 4, "text": "e", "status": "ok", "content": "E ok"},
        ],
    }
    result = await join_node(state)
    assert result.get("error") is None, "partial failure should not set error"
    msg = result["messages"][0]["content"]
    assert "A ok" in msg
    assert "timeout" in msg or "timed out" in msg or "failed" in msg.lower()
    assert "4 of 5 subtasks completed" in msg


@pytest.mark.asyncio
async def test_partial_failure_all_workers_fail_turn_errors():
    """Best-effort: all 5 fail, state.error is set."""
    state = {
        "worker_results": [
            {"idx": 0, "text": "a", "status": "failed", "error": "timeout"},
            {"idx": 1, "text": "b", "status": "failed", "error": "crash"},
            {"idx": 2, "text": "c", "status": "failed", "error": "429"},
            {"idx": 3, "text": "d", "status": "failed", "error": "timeout"},
            {"idx": 4, "text": "e", "status": "failed", "error": "error"},
        ],
    }
    result = await join_node(state)
    assert result.get("error") is not None
    assert "All fan-out workers failed" in result["error"]
    assert "timeout" in result["error"]
    assert "crash" in result["error"]
    # The message still shows all sections
    msg = result["messages"][0]["content"]
    assert "subtask" in msg.lower()


# ─── Worker timeout ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_timeout_marks_status_timeout():
    """A worker that exceeds WORKER_TIMEOUT_SECONDS gets status: timeout."""
    with (
        patch("core.nodes.worker._send_to_backend", _mock_send_slow(delay=999)),
        patch("core.nodes.worker.WORKER_TIMEOUT_SECONDS", 0.05),
    ):
        result = await worker_node(_worker_state(subtask_idx=0))
    entry = result["worker_results"][0]
    assert entry["status"] == "timeout"
    assert "exceeded" in entry["error"]


# ─── Worker 429 detection ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_429_marks_status_failed_rate_limit():
    """When _send_to_backend raises a 429, status is 'failed' with error 'rate_limit'."""
    import aiohttp

    async def _raise_429(_messages, _backend):
        raise aiohttp.ClientResponseError(
            request_info=None,
            history=None,
            status=429,
            message="Too Many Requests",
            headers={},
        )

    with patch("core.nodes.worker._send_to_backend", _raise_429):
        result = await worker_node(_worker_state(subtask_idx=0))
    entry = result["worker_results"][0]
    assert entry["status"] == "failed"
    assert entry["error"] == "rate_limit"


# ─── 429 mid-fan-out: in-flight workers continue ─────────────────────────

@pytest.mark.asyncio
async def test_429_mid_fanout_inflight_workers_continue():
    """One worker 429s; the others complete normally. All results present."""
    import aiohttp

    call_count = 0

    async def _alternating(_messages, _backend):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise aiohttp.ClientResponseError(
                request_info=None, history=None,
                status=429, message="Too Many Requests", headers={},
            )
        return f"result-{call_count}"

    with patch("core.nodes.worker._send_to_backend", _alternating):
        # Simulate 3 independent workers
        r1 = await worker_node(_worker_state(subtask_idx=0))
        r2 = await worker_node(_worker_state(subtask_idx=1))
        r3 = await worker_node(_worker_state(subtask_idx=2))

    all_results = r1["worker_results"] + r2["worker_results"] + r3["worker_results"]
    assert len(all_results) == 3
    failed = [r for r in all_results if r["status"] == "failed"]
    ok = [r for r in all_results if r["status"] == "ok"]
    assert len(failed) == 1
    assert failed[0]["error"] == "rate_limit"
    assert len(ok) == 2
