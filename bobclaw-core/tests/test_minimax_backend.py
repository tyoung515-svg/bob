"""
BoBClaw Core — Unit tests for MiniMaxClient and the minimax think-strip path

All network I/O is mocked; no live API calls.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.minimax import MiniMaxClient
from core.nodes.execute import _default_send_to_backend


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client(api_key: str = "dummy-key", base_url: str | None = None) -> MiniMaxClient:
    return MiniMaxClient(api_key=api_key, base_url=base_url)


# ─── Construction ─────────────────────────────────────────────────────────────

def test_default_base_url_from_config():
    client = _make_client()
    assert client.base_url == "https://api.minimax.io/v1"


def test_custom_base_url_override():
    client = _make_client(base_url="https://custom.minimax.io/v1")
    assert client.base_url == "https://custom.minimax.io/v1"


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
async def test_health_check_returns_false_on_exception():
    client = _make_client()

    with patch("aiohttp.ClientSession.get", side_effect=Exception("connection failed")):
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


# ─── list_models ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_models_parses_data_ids():
    client = _make_client()
    fake_data = {"data": [{"id": "MiniMax-M3"}, {"id": "MiniMax-Text-01"}]}
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value=fake_data)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_cm):
        models = await client.list_models()

    assert models == ["MiniMax-M3", "MiniMax-Text-01"]


@pytest.mark.asyncio
async def test_list_models_returns_empty_on_empty_key():
    client = _make_client(api_key="")
    client.api_key = ""
    models = await client.list_models()
    assert models == []


@pytest.mark.asyncio
async def test_list_models_returns_empty_on_error():
    client = _make_client()

    with patch("aiohttp.ClientSession.get", side_effect=Exception("API error")):
        models = await client.list_models()

    assert models == []


# ─── chat ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_posts_to_chat_completions_with_bearer():
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": "Hello from MiniMax"}}]
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
        result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="MiniMax-M3")

    assert result == fake_response
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "MiniMax-M3"
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
            model="MiniMax-M3",
            temperature=None,
        )

    assert "temperature" not in captured["json"]


@pytest.mark.asyncio
async def test_chat_with_kwarg_zero_included():
    """Zero-valued kwargs (e.g. temperature=0) are NOT filtered out (pins task 01 item 3)."""
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
            model="MiniMax-M3",
            temperature=0,
        )

    assert captured["json"]["temperature"] == 0


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
async def test_stream_chat_skips_keepalives():
    """SSE keep-alive lines (data: with no valid content) are skipped."""
    client = _make_client()

    sse_lines = [
        b"data:\n\n",
        b'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n',
        b"data: :keepalive\n\n",
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

    assert chunks == ["Hi"]


@pytest.mark.asyncio
async def test_stream_chat_skips_malformed_chunks():
    """Malformed SSE data lines (invalid JSON) are skipped without crashing."""
    client = _make_client()

    sse_lines = [
        b"data: {invalid json}\n\n",
        b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n',
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

    assert chunks == ["OK"]


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


# ─── Think-strip (execute._default_send_to_backend) ───────────────────────────

@pytest.mark.asyncio
async def test_send_to_backend_strips_think_block():
    """_default_send_to_backend strips leading <think>...</think> for minimax."""
    mock_chat = AsyncMock(return_value={
        "choices": [{"message": {"content": "<think>Let me reason this through step by step.</think>The answer is 42."}}]
    })
    with patch("core.backends.minimax.MiniMaxClient.chat", mock_chat):
        result = await _default_send_to_backend(
            messages=[{"role": "user", "content": "hello"}],
            backend="minimax",
        )
    assert result == "The answer is 42."


@pytest.mark.asyncio
async def test_send_to_backend_passes_through_without_think_block():
    """_default_send_to_backend preserves content with no think block."""
    mock_chat = AsyncMock(return_value={
        "choices": [{"message": {"content": "Just a normal answer."}}]
    })
    with patch("core.backends.minimax.MiniMaxClient.chat", mock_chat):
        result = await _default_send_to_backend(
            messages=[{"role": "user", "content": "hello"}],
            backend="minimax",
        )
    assert result == "Just a normal answer."


@pytest.mark.asyncio
async def test_send_to_backend_handles_none_content():
    """_default_send_to_backend does not crash when content is None."""
    mock_chat = AsyncMock(return_value={
        "choices": [{"message": {"content": None}}]
    })
    with patch("core.backends.minimax.MiniMaxClient.chat", mock_chat):
        result = await _default_send_to_backend(
            messages=[{"role": "user", "content": "hello"}],
            backend="minimax",
        )
    assert result == ""
