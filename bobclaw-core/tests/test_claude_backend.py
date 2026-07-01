"""
BoBClaw Core — Unit tests for ClaudeClient

All network I/O is mocked; no live API calls.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.claude import ClaudeClient


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client(
    api_key: str = "sk-test", base_url: str | None = None
) -> ClaudeClient:
    return ClaudeClient(api_key=api_key, base_url=base_url)


# ─── Construction ─────────────────────────────────────────────────────────────

def test_default_base_url():
    client = _make_client()
    assert client.base_url == "https://api.anthropic.com"


def test_custom_base_url_override():
    client = _make_client(base_url="https://custom.anthropic.com/v1")
    assert client.base_url == "https://custom.anthropic.com/v1"


# ─── health_check ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_health_check_returns_false_when_api_key_empty():
    client = _make_client(api_key="")
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
async def test_chat_posts_to_messages_with_anthropic_headers():
    client = _make_client(api_key="sk-test")
    fake_response = {
        "content": [{"type": "text", "text": "Hello from Claude"}]
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
        result = await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="claude-sonnet-4-6",
        )

    assert result == fake_response
    assert captured["url"].endswith("/v1/messages")
    assert captured["headers"]["x-api-key"] == "sk-test"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in captured["headers"]
    assert captured["json"]["model"] == "claude-sonnet-4-6"
    assert captured["json"]["messages"][0]["content"] == "hi"
    assert captured["json"]["max_tokens"] == 4096
    assert "stream" not in captured["json"]


@pytest.mark.asyncio
async def test_chat_with_system_kwarg_puts_system_at_top_level():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"content": []})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[{"role": "user", "content": "hello"}],
            model="claude-test",
            system="You are a tester.\n\nBe concise.",
        )

    assert captured["json"]["system"] == "You are a tester.\n\nBe concise."


@pytest.mark.asyncio
async def test_chat_without_system_kwarg_omits_system_key():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"content": []})
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
            model="claude-test",
        )

    assert "system" not in captured["json"]


@pytest.mark.asyncio
async def test_chat_with_system_none_omits_system_key():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"content": []})
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
            model="claude-test",
            system=None,
        )

    assert "system" not in captured["json"]


@pytest.mark.asyncio
async def test_chat_with_system_empty_string_includes_key():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"content": []})
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
            model="claude-test",
            system="",
        )

    assert captured["json"]["system"] == ""


# ─── stream_chat ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_chat_decodes_content_block_delta():
    client = _make_client()

    sse_lines = [
        b'event: content_block_delta\n',
        b'data: {"type":"content_block_delta","delta":{"text":"Hello"}}\n\n',
        b'event: content_block_delta\n',
        b'data: {"type":"content_block_delta","delta":{"text":" world"}}\n\n',
        b'event: message_stop\n',
        b'data: {"type":"message_stop"}\n\n',
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
        async for chunk in client.stream_chat(
            messages=[{"role": "user", "content": "say hello"}]
        ):
            chunks.append(chunk)

    assert chunks == ["Hello", " world"]


@pytest.mark.asyncio
async def test_stream_chat_with_system_none_omits_system_key():
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
            model="claude-test",
            system=None,
        ):
            pass

    assert "system" not in captured["json"]
    assert captured["json"]["stream"] is True
