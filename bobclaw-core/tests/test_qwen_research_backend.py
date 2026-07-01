"""
BoBClaw Core — Unit tests for QwenResearchClient

All network I/O is mocked; no live API calls.
Covers the local-server deviation: health_check returns True on 200 even with an empty api_key,
and no Authorization header when key is empty.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.qwen_research import QwenResearchClient


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client(api_key: str = "dummy-key", base_url: str | None = None) -> QwenResearchClient:
    return QwenResearchClient(api_key=api_key, base_url=base_url)


# ─── Construction ─────────────────────────────────────────────────────────────

def test_default_base_url_from_config():
    client = _make_client()
    # Default base_url should be a local URL ending with '/v1'
    assert client.base_url.endswith("/v1")
    assert "127.0.0.1" in client.base_url


def test_custom_base_url_override():
    client = _make_client(base_url="https://custom.qwen.example.com/v1")
    assert client.base_url == "https://custom.qwen.example.com/v1"


# ─── health_check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_returns_true_when_api_key_empty_and_status_200():
    """Local deviation: empty API key still allows health check to succeed on 200."""
    client = _make_client(api_key="")
    client.api_key = ""  # enforce empty (constructor may fall back to config)
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_cm):
        assert await client.health_check() is True


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
async def test_health_check_returns_false_on_exception():
    client = _make_client()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(side_effect=Exception("Connection refused"))
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_cm):
        assert await client.health_check() is False


@pytest.mark.asyncio
async def test_health_check_returns_true_on_200_with_key():
    client = _make_client(api_key="test-key")
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
        "choices": [{"message": {"content": "Hello from Qwen"}}]
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
        result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="qwen-test")

    assert result == fake_response
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "qwen-test"
    assert captured["json"]["messages"][0]["content"] == "hi"
    assert captured["json"]["stream"] is False


@pytest.mark.asyncio
async def test_chat_posts_no_auth_when_key_empty():
    """Local deviation: empty API key -> no Authorization header sent."""
    client = _make_client(api_key="")
    client.api_key = ""  # enforce empty
    fake_response = {
        "choices": [{"message": {"content": "Hello"}}]
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
        result = await client.chat(messages=[{"role": "user", "content": "hi"}])

    assert result == fake_response
    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_chat_model_default():
    """Check that when no model is supplied, the default from config is used."""
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": ""}}]
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(messages=[{"role": "user", "content": "hi"}])

    # Default model should be something (from config, but we trust the client uses config default)
    # We simply assert that 'model' is present in the payload.
    assert "model" in captured["json"]


@pytest.mark.asyncio
async def test_chat_model_override():
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": ""}}]
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(messages=[{"role": "user", "content": "hi"}], model="qwen-custom")

    assert captured["json"]["model"] == "qwen-custom"


@pytest.mark.asyncio
async def test_chat_passes_tools_kwarg():
    """Load-bearing: tools kwarg must flow through to the POST body."""
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": ""}}]
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    tools_param = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=tools_param,
            tool_choice="auto"
        )

    assert "tools" in captured["json"]
    assert captured["json"]["tools"] == tools_param
    assert captured["json"]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_chat_keeps_temperature_zero():
    """temperature=0 is kept because 0 is not None."""
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": ""}}]
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(messages=[{"role": "user", "content": "hi"}], temperature=0)

    assert "temperature" in captured["json"]
    assert captured["json"]["temperature"] == 0


@pytest.mark.asyncio
async def test_chat_omits_none_kwargs():
    """None-valued kwargs are omitted from the POST body."""
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": ""}}]
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(messages=[{"role": "user", "content": "hi"}], temperature=None)

    assert "temperature" not in captured["json"]


@pytest.mark.asyncio
async def test_chat_with_kwarg_empty_string_included():
    """An empty string kwarg is kept (not None)."""
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": ""}}]
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(messages=[{"role": "user", "content": "hi"}], stop="")

    assert captured["json"]["stop"] == ""


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
