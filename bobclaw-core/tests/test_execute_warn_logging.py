"""
BoBClaw Core — Unit tests for warn-mode logging in _default_send_to_backend
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from core.nodes.execute import _default_send_to_backend


@pytest.mark.asyncio
async def test_kimi_platform_warn_mode_logs_warning(caplog):
    with (
        patch("core.nodes.execute.check_cap", return_value=(True, 1.50, "warn")),
        patch("core.backends.kimi_platform.KimiPlatformClient") as MockKPC,
        patch("core.nodes.execute.track_cost"),
    ):
        mock_instance = MockKPC.return_value
        mock_instance.chat = AsyncMock(
            return_value={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        )

        with caplog.at_level(logging.WARNING, logger="core.nodes.execute"):
            await _default_send_to_backend(
                messages=[{"role": "user", "content": "hi"}],
                backend="kimi_platform",
            )

    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    msg = caplog.records[0].getMessage()
    assert "PAYG daily spend" in msg
    assert "$1.50" in msg
    assert "warn threshold" in msg


@pytest.mark.asyncio
async def test_execute_blocks_on_kimi_cap_reached():
    """When the daily cap is reached, execute_node returns the cap message
    without calling KimiPlatformClient.chat."""
    from core.nodes.execute import execute_node

    with (
        patch("core.nodes.execute.check_cap", return_value=(False, 25.00, "block")),
        patch("core.backends.kimi_platform.KimiPlatformClient") as MockKPC,
        patch(
            "core.nodes.execute._check_escalation_pin",
            new=AsyncMock(return_value=None),
        ),
        patch("core.nodes.execute.config") as mock_config,
    ):
        mock_config.KIMI_PLATFORM_DAILY_USD_LIMIT = 20.00
        mock_config.REDIS_URL = "redis://localhost:6379/0"
        result = await execute_node({
            "task": "hi",
            "backend": "kimi_platform",
            "messages": [],
        })

    # No backend call when blocked
    MockKPC.return_value.chat.assert_not_called()

    msgs = result.get("messages", [])
    assert msgs, "execute_node returned no message"
    content = msgs[-1]["content"]
    assert "daily cap reached" in content
    assert "$25.00" in content
    assert "$20.00" in content
    assert result.get("error") is None


@pytest.mark.asyncio
async def test_execute_tracks_cost_on_kimi_platform_success():
    """On a successful kimi_platform call, track_cost is invoked with
    the parsed usage (input minus cached, cached, output)."""
    from core.nodes.execute import execute_node

    with (
        patch("core.nodes.execute.check_cap", return_value=(True, 1.00, "ok")),
        patch(
            "core.nodes.execute._check_escalation_pin",
            new=AsyncMock(return_value=None),
        ),
        patch("core.backends.kimi_platform.KimiPlatformClient") as MockKPC,
        patch("core.nodes.execute.track_cost") as mock_track,
    ):
        mock_instance = MockKPC.return_value
        mock_instance.chat = AsyncMock(return_value={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cached_tokens": 20,
            },
        })
        await execute_node({
            "task": "hi",
            "backend": "kimi_platform",
            "messages": [],
        })

    mock_track.assert_called_once()
    kwargs = mock_track.call_args.kwargs
    # parse_usage subtracts cached_tokens from prompt_tokens
    assert kwargs["input_tokens"] == 80
    assert kwargs["cached_tokens"] == 20
    assert kwargs["output_tokens"] == 50
