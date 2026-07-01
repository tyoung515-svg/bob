"""
BoBClaw Core — Observation pass for producer/critic discipline (dispatch_009).

Captures realistic-shape bobclaw.core.fanout log lines for a 5-worker fan-out
with critic enabled, using mocked _send_to_backend with injected latency.

Output is consumed by worker/handoff_009_observation_findings.md (report).

NOT a unit test of correctness — that's tests/test_fanout_critic.py. This test
asserts log-line shape AND prints captured lines for downstream analysis.
"""
from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import patch

import pytest

from core.config import MAX_WORKER_USD_BY_BACKEND
from core.nodes.join import join_node
from core.nodes.worker import worker_node


# Deterministic mock of _send_to_backend:
#   - Producer call: returns "done: subtask <task>" after 50ms sleep.
#   - Critic call: returns a JSON verdict after 30ms sleep, cycling through
#     approve / flag / reject / approve / approve so we hit all branches.
async def _mock_send_to_backend(messages, backend):
    last_user = next((m for m in reversed(messages) if m["role"] == "user"), None)
    content = last_user["content"] if last_user else ""

    # Heuristic: critic prompts contain the literal substring "Worker output:"
    is_critic = "Worker output:" in content

    if is_critic:
        await asyncio.sleep(0.03)
        if "subtask 4" in content:
            return '{"verdict":"reject","reasons":["fabricated file path"]}'
        if "subtask 3" in content:
            return '{"verdict":"flag","reasons":["scope drift"]}'
        return '{"verdict":"approve","reasons":[]}'

    await asyncio.sleep(0.05)
    return f"done: {content}"


@pytest.mark.asyncio
async def test_observation_critic_on_five_workers(caplog):
    caplog.set_level(logging.INFO, logger="bobclaw.core.fanout")

    sub_states = [
        {
            "task": f"subtask {i}",
            "backend": "kimi_code",
            "face_id": "worker-kimi",
            "subtask_idx": i,
            "turn_id": "obs-turn-001",
            "critic_backend": "claude_api",
            "critic_prompt_template": None,
        }
        for i in range(5)
    ]

    with patch("core.nodes.worker._send_to_backend", new=_mock_send_to_backend), \
         patch("core.nodes.critic._send_to_backend", new=_mock_send_to_backend):
        results = await asyncio.gather(*(worker_node(s) for s in sub_states))

    entries = [r["worker_results"][0] for r in results]
    assert len(entries) == 5

    verdicts = [e["critic_verdict"] for e in entries]
    assert verdicts.count("approve") == 3
    assert verdicts.count("flag") == 1
    assert verdicts.count("reject") == 1

    rejected = [e for e in entries if e["critic_verdict"] == "reject"]
    assert len(rejected) == 1
    assert rejected[0]["status"] == "rejected"
    assert "critic_rejected" in rejected[0]["error"]

    fanout_lines = [
        json.loads(rec.message)
        for rec in caplog.records
        if rec.name == "bobclaw.core.fanout"
    ]
    assert len(fanout_lines) == 5

    for line in fanout_lines:
        assert "critic_backend" in line and line["critic_backend"] == "claude_api"
        assert "critic_verdict" in line
        assert "critic_duration_ms" in line and line["critic_duration_ms"] >= 0
        assert "critic_reasons_count" in line

    join_result = await join_node({
        "worker_results": entries,
        "messages": [],
        "task": "observation turn",
    })
    joined_msg = (
        join_result.get("messages", [{}])[-1].get("content", "")
        if join_result.get("messages")
        else ""
    )
    assert "rejected" in joined_msg.lower() or "rejected" in str(join_result).lower()
    assert "flag" in str(join_result).lower()

    per_worker_cost = (
        MAX_WORKER_USD_BY_BACKEND["kimi_code"] +
        MAX_WORKER_USD_BY_BACKEND["claude_api"]
    )
    total_cost_estimate = 5 * per_worker_cost

    summary = {
        "fanout_lines": fanout_lines,
        "verdict_distribution": {
            "approve": verdicts.count("approve"),
            "flag": verdicts.count("flag"),
            "reject": verdicts.count("reject"),
        },
        "total_cost_estimate_usd": total_cost_estimate,
        "per_worker_cost_usd": per_worker_cost,
        "critic_durations_ms": [line["critic_duration_ms"] for line in fanout_lines],
        "worker_durations_ms": [line["duration_ms"] for line in fanout_lines],
    }
    print("\n=== OBSERVATION SUMMARY ===")
    print(json.dumps(summary, indent=2))
    print("=== END OBSERVATION SUMMARY ===\n")
