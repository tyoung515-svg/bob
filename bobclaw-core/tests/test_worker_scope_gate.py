"""
BoBClaw Core — Scope-drift gate on the fan-out worker path (GR-P4).

All critic calls are mocked; no network traffic.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from core.nodes.dispatch import _route_after_dispatch, dispatch_node
from core.nodes.gate import WORKER_SCOPE_REVIEW_PROMPT
from core.nodes.worker import worker_node


@pytest.fixture
def _patch_backend():
    """Patch the worker's backend send to return a fixed response."""
    with patch(
        "core.nodes.worker._send_to_backend",
        new_callable=AsyncMock,
        return_value="worker output",
    ) as mock:
        yield mock


@pytest.fixture
def _scope():
    return {
        "branch": "feat/test",
        "may_touch": ["src/**", "tests/**"],
        "may_not_touch": [".secrets/**", "main"],
        "auto_actions": ["read", "write_in_scope", "run_tests"],
        "escalate_actions": ["schema_change", "dep_install"],
    }


def _sub_state(scope=None, task="do the thing"):
    return {
        "task": task,
        "backend": "kimi_code",
        "subtask_idx": 0,
        "critic_backend": "minimax",
        "critic_prompt_template": None,
        "scope": scope,
    }


@pytest.mark.asyncio
async def test_worker_scope_approve_is_auto(_patch_backend, _scope):
    with patch(
        "core.nodes.worker.run_critic",
        new_callable=AsyncMock,
        return_value=("approve", ["in scope"]),
    ):
        result = await worker_node(_sub_state(scope=_scope))

    entry = result["worker_results"][0]
    assert entry["status"] == "ok"
    assert entry["gate_destination"] == "auto"
    assert entry["gate_reasons"] == ["in scope"]
    assert entry["critic_verdict"] == "approve"


@pytest.mark.asyncio
async def test_worker_scope_flag_is_gate(_patch_backend, _scope):
    with patch(
        "core.nodes.worker.run_critic",
        new_callable=AsyncMock,
        return_value=("flag", ["minor drift"]),
    ):
        result = await worker_node(_sub_state(scope=_scope))

    entry = result["worker_results"][0]
    assert entry["status"] == "flagged"
    assert entry["gate_destination"] == "gate"
    assert entry["gate_reasons"] == ["minor drift"]
    assert entry["critic_verdict"] == "flag"


@pytest.mark.asyncio
async def test_worker_scope_reject_is_human(_patch_backend, _scope):
    with patch(
        "core.nodes.worker.run_critic",
        new_callable=AsyncMock,
        return_value=("reject", ["touches .secrets"]),
    ):
        result = await worker_node(_sub_state(scope=_scope))

    entry = result["worker_results"][0]
    assert entry["status"] == "rejected"
    assert entry["gate_destination"] == "human"
    assert entry["gate_reasons"] == ["touches .secrets"]
    assert entry["error"] == "critic_rejected: touches .secrets"


@pytest.mark.asyncio
async def test_worker_scope_critic_none_fails_closed_human(_patch_backend, _scope):
    with patch(
        "core.nodes.worker.run_critic",
        new_callable=AsyncMock,
        return_value=("none", ["critic_unavailable: timeout"]),
    ):
        result = await worker_node(_sub_state(scope=_scope))

    entry = result["worker_results"][0]
    assert entry["status"] == "flagged"
    assert entry["gate_destination"] == "human"
    assert "critic_unavailable" in entry["gate_reasons"][0]


@pytest.mark.asyncio
async def test_worker_without_scope_uses_generic_critic(_patch_backend):
    with patch(
        "core.nodes.worker.run_critic",
        new_callable=AsyncMock,
        return_value=("approve", ["looks good"]),
    ) as critic_mock:
        result = await worker_node(_sub_state(scope=None))

    entry = result["worker_results"][0]
    assert entry["status"] == "ok"
    assert "gate_destination" not in entry
    assert entry["critic_verdict"] == "approve"
    # Generic prompt should be the default None -> CRITIC_DEFAULT_PROMPT_TEMPLATE
    _, kwargs = critic_mock.call_args
    assert kwargs["prompt_template"] is None


@pytest.mark.asyncio
async def test_worker_scope_reaches_critic_prompt(_patch_backend, _scope):
    captured = {}

    async def _capture(*, subtask_text, worker_output, critic_backend, prompt_template):
        captured.update(locals())
        return ("approve", ["in scope"])

    with patch("core.nodes.worker.run_critic", side_effect=_capture):
        await worker_node(_sub_state(scope=_scope, task="refactor src/module.py"))

    assert captured["prompt_template"] is WORKER_SCOPE_REVIEW_PROMPT
    assert ".secrets/**" in captured["subtask_text"]
    assert "may_not_touch" in captured["subtask_text"]
    assert "refactor src/module.py" in captured["subtask_text"]
    assert captured["worker_output"] == "worker output"


def test_dispatch_threads_scope_into_worker_sub_state():
    scope = {"may_touch": ["src/**"], "may_not_touch": [".secrets/**"]}
    state = {
        "task": "implement the thing",
        "face_id": "worker-kimi",
        "backend": "kimi_code",
        "messages": [],
        "subtasks": ["a", "b", "c", "d", "e"],
        "fanout_width": None,
        "escalation_backend": None,
        "recalled_facts": [],
        "scope": scope,
    }
    delta = dispatch_node(state)
    state.update(delta)
    route = _route_after_dispatch(state)

    assert isinstance(route, list)
    assert len(route) == 5
    for send in route:
        assert send.arg.get("scope") is scope
