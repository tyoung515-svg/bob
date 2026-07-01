"""
BoBClaw Core — Unit tests for GeminiClient

All network I/O is mocked; no live API calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.gemini import GeminiClient


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client(api_key: str = "dummy-key") -> GeminiClient:
    return GeminiClient(api_key=api_key)


# ─── Construction ─────────────────────────────────────────────────────────────

def test_default_api_base():
    client = _make_client()
    assert client.API_BASE == "https://generativelanguage.googleapis.com/v1beta"


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
async def test_chat_posts_to_generate_content_with_api_key_header():
    client = _make_client(api_key="AIza-test")
    fake_response = {
        "candidates": [{"content": {"parts": [{"text": "Hello from Gemini"}]}}]
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
            model="gemini-3-flash-preview",
        )

    assert result == fake_response
    assert captured["url"].endswith(":generateContent")
    assert captured["headers"]["x-goog-api-key"] == "AIza-test"
    assert captured["json"]["contents"][0]["parts"][0]["text"] == "hi"


@pytest.mark.asyncio
async def test_chat_converts_system_message_to_system_instruction():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "hello"},
            ],
            model="gemini-3-flash-preview",
        )

    assert captured["json"]["system_instruction"]["parts"][0]["text"] == "You are a helpful assistant."
    assert captured["json"]["contents"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_chat_converts_assistant_role_to_model():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"candidates": [{"content": {"parts": [{"text": ""}]}}]})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[
                {"role": "user", "content": "what is 2+2"},
                {"role": "assistant", "content": "4"},
            ],
            model="gemini-3-flash-preview",
        )

    assert captured["json"]["contents"][1]["role"] == "model"


@pytest.mark.asyncio
async def test_chat_response_extracts_text_from_parts():
    client = _make_client()
    fake_response = {
        "candidates": [{"content": {"parts": [{"text": "Hello"}, {"text": " world"}]}}]
    }
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_response)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        raw = await client.chat(
            messages=[{"role": "user", "content": "say hello"}],
            model="gemini-3-flash-preview",
        )

    texts = [p["text"] for p in raw["candidates"][0]["content"]["parts"] if p.get("text")]
    assert "".join(texts) == "Hello world"


# ─── stream_chat ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_chat_decodes_sse_parts():
    client = _make_client()

    sse_lines = [
        b'{"candidates":[{"content":{"parts":[{"text":"Hello"}]}}]}\n\n',
        b'{"candidates":[{"content":{"parts":[{"text":" world"}]}}]}\n\n',
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
