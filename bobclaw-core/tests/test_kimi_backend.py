"""
BoBClaw Core — Unit tests for KimiClient

All network I/O is mocked; no live API calls.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.kimi import KimiClient


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client(api_key: str = "dummy-key", base_url: str | None = None) -> KimiClient:
    return KimiClient(api_key=api_key, base_url=base_url)


# ─── Construction ─────────────────────────────────────────────────────────────

def test_default_base_url_is_moonshot_v1():
    client = _make_client()
    assert client.base_url == "https://api.moonshot.ai/v1"


def test_custom_base_url_override():
    client = _make_client(base_url="https://custom.kimi.com/v1")
    assert client.base_url == "https://custom.kimi.com/v1"


def test_default_model_is_real_api_id_not_ide_slug():
    from core.config import config
    assert config.KIMI_MODEL == "kimi-k2.7-code"
    assert config.KIMI_MODEL != "kimi-for-coding"


# ─── health_check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_returns_false_when_api_key_empty():
    client = _make_client(api_key="")
    client.api_key = ""  # constructor falls back to config when key is falsy; force empty
    assert await client.health_check() is False


@pytest.mark.asyncio
async def test_health_check_returns_false_on_http_error():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.status = 401
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_cm):
        assert await client.health_check() is False


@pytest.mark.asyncio
async def test_health_check_returns_true_on_200():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_cm):
        assert await client.health_check() is True


# ─── chat ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_posts_to_chat_completions_with_bearer():
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": "Hello from Kimi"}}]
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="kimi-k2.7-code")

    assert result == fake_response
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "kimi-k2.7-code"
    assert captured["json"]["messages"][0]["content"] == "hi"
    assert captured["json"]["stream"] is False


@pytest.mark.asyncio
async def test_chat_with_kwarg_none_omits_key():
    client = _make_client(api_key="sk-test")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="kimi-k2.7-code",
            temperature=None,
        )

    assert "temperature" not in captured["json"]


@pytest.mark.asyncio
async def test_chat_with_kwarg_empty_string_includes_key():
    client = _make_client(api_key="sk-test")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="kimi-k2.7-code",
            stop="",
        )

    assert captured["json"]["stop"] == ""


@pytest.mark.asyncio
async def test_chat_strips_temperature_zero():
    """Regression: temperature=0 must not reach the membership endpoint."""
    client = _make_client(api_key="sk-test")
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="kimi-k2.7-code",
            temperature=0,
        )

    assert "temperature" not in captured["json"]


# ─── stream_chat ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_chat_decodes_sse_deltas():
    client = _make_client()

    sse_lines = [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    async def _async_iter(lines):
        for line in lines:
            yield line

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = _async_iter(sse_lines)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        chunks = []
        async for chunk in client.stream_chat(messages=[{"role": "user", "content": "say hello"}]):
            chunks.append(chunk)

    assert chunks == ["Hello", " world"]


@pytest.mark.asyncio
async def test_stream_chat_with_kwarg_none_omits_key():
    client = _make_client()

    async def _async_iter(lines):
        for line in lines:
            yield line

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = _async_iter([])
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        async for _ in client.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            temperature=None,
        ):
            pass

    assert "temperature" not in captured["json"]
    assert captured["json"]["stream"] is True


@pytest.mark.asyncio
async def test_stream_chat_strips_temperature_zero():
    """Regression: temperature=0 must not reach the membership endpoint."""
    client = _make_client()

    async def _async_iter(lines):
        for line in lines:
            yield line

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = _async_iter([])
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        async for _ in client.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            temperature=0,
        ):
            pass

    assert "temperature" not in captured["json"]
    assert captured["json"]["stream"] is True
