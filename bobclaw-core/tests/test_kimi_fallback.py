"""
BoBClaw Core — Unit tests for Kimi 429 fallback + escalation pins
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import aiohttp
import pytest

from core.backends import _cost as cost
from core.nodes import execute as execute_module


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _reset_cost():
    cost._DAILY_USD.clear()


# ─── 429 fallback ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_429_on_kimi_code_triggers_escalation_to_kimi_platform(mock_redis):
    _reset_cost()

    call_count = 0

    async def _mock_stream(messages, backend, model_override=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=429,
            )
        yield "escalated response"

    with patch.object(execute_module, "_stream_to_backend", _mock_stream):
        result = await execute_module.execute_node(
            {
                "task": "do something",
                "backend": "kimi_code",
                "messages": [],
                "escalation_backend": "kimi_platform",
            }
        )

    assert result["messages"][0]["content"] == "escalated response"
    mock_redis.set.assert_called_once_with(
        "bobclaw:pin:kimi_code", "kimi_platform", ex=1800,
    )


@pytest.mark.asyncio
async def test_pinned_call_skips_original_backend(mock_redis):
    _reset_cost()

    # Pin is active
    mock_redis.get.return_value = "kimi_platform"

    calls = []

    async def _track(messages, backend, model_override=None):
        calls.append(backend)
        yield f"from {backend}"

    with patch.object(execute_module, "_stream_to_backend", _track):
        result = await execute_module.execute_node(
            {
                "task": "do something",
                "backend": "kimi_code",
                "messages": [],
                "escalation_backend": "kimi_platform",
            }
        )

    # Because of the pin, _check_escalation_pin returns "kimi_platform",
    # so _stream_to_backend is called once with "kimi_platform".
    assert calls == ["kimi_platform"]
    assert result["messages"][0]["content"] == "from kimi_platform"


@pytest.mark.asyncio
async def test_after_ttl_expiry_kimi_code_is_retried(mock_redis):
    _reset_cost()

    # No pin active — mock_redis.get.return_value is already None

    calls = []

    async def _track(messages, backend, model_override=None):
        calls.append(backend)
        yield f"from {backend}"

    with patch.object(execute_module, "_stream_to_backend", _track):
        result = await execute_module.execute_node(
            {
                "task": "do something",
                "backend": "kimi_code",
                "messages": [],
                "escalation_backend": "kimi_platform",
            }
        )

    # No pin, so original backend is used
    assert calls == ["kimi_code"]
    assert result["messages"][0]["content"] == "from kimi_code"


# ─── Face escalation backend resolution ───────────────────────────────────────


def test_worker_kimi_escalation_backend_is_kimi_platform():
    from core.faces.registry import FaceRegistry

    face = FaceRegistry().get_face("worker-kimi")
    assert face.escalation_backend == "kimi_platform"
