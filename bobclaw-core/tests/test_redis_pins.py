"""
BoBClaw Core — Unit tests for Redis-backed escalation pins
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from core.nodes.execute import (
    _check_escalation_pin,
    _pin_escalation,
    _pin_key,
)


@pytest.fixture(autouse=True)
def _reset_warning_flag():
    import core.nodes.execute as ex
    ex._redis_warned_first_failure = False


@pytest.mark.asyncio
async def test_pin_escalation_sets_redis_key_with_ttl():
    client = AsyncMock()
    client.set = AsyncMock(return_value=True)
    with patch("core.nodes.execute._get_redis", return_value=client):
        await _pin_escalation("kimi_code", "kimi_platform", ttl_seconds=1800)

    client.set.assert_called_once_with(
        _pin_key("kimi_code"), "kimi_platform", ex=1800,
    )


@pytest.mark.asyncio
async def test_check_escalation_pin_returns_value_when_present():
    client = AsyncMock()
    client.get = AsyncMock(return_value="kimi_platform")
    with patch("core.nodes.execute._get_redis", return_value=client):
        result = await _check_escalation_pin("kimi_code")

    assert result == "kimi_platform"


@pytest.mark.asyncio
async def test_check_escalation_pin_returns_none_when_missing():
    client = AsyncMock()
    client.get = AsyncMock(return_value=None)
    with patch("core.nodes.execute._get_redis", return_value=client):
        result = await _check_escalation_pin("kimi_code")

    assert result is None


@pytest.mark.asyncio
async def test_check_escalation_pin_returns_none_on_redis_failure(caplog):
    client = AsyncMock()
    client.get = AsyncMock(side_effect=Exception("nope"))
    with patch("core.nodes.execute._get_redis", return_value=client):
        with caplog.at_level(logging.WARNING, logger="core.nodes.execute"):
            result = await _check_escalation_pin("kimi_code")

    assert result is None
    assert any("Redis pin read failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_pin_escalation_silent_on_redis_failure(caplog):
    client = AsyncMock()
    client.set = AsyncMock(side_effect=Exception("nope"))
    with patch("core.nodes.execute._get_redis", return_value=client):
        with caplog.at_level(logging.WARNING, logger="core.nodes.execute"):
            await _pin_escalation("kimi_code", "kimi_platform", ttl_seconds=300)

    assert any("Redis pin write failed" in r.getMessage() for r in caplog.records)
