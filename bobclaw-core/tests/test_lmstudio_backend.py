"""
BoBClaw Core — Unit tests for LMStudioClient

All network I/O is mocked; no live API calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.lmstudio import LMStudioClient


def _make_client(base_url: str | None = None) -> LMStudioClient:
    return LMStudioClient(base_url=base_url)


@pytest.mark.asyncio
async def test_chat_with_kwarg_none_omits_key():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, **kwargs):
        captured["json"] = kwargs["json"]
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="lm-studio-model",
            temperature=None,
        )

    assert "temperature" not in captured["json"]


@pytest.mark.asyncio
async def test_chat_with_kwarg_empty_string_includes_key():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, **kwargs):
        captured["json"] = kwargs["json"]
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        await client.chat(
            messages=[{"role": "user", "content": "hi"}],
            model="lm-studio-model",
            stop="",
        )

    assert captured["json"]["stop"] == ""


@pytest.mark.asyncio
async def test_stream_chat_with_kwarg_none_omits_key():
    client = _make_client()

    async def _sse():
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield b"data: [DONE]\n\n"

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = _sse()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, **kwargs):
        captured["json"] = kwargs["json"]
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        chunks = []
        async for chunk in client.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            model="lm-studio-model",
            temperature=None,
        ):
            chunks.append(chunk)

    assert chunks == ["ok"]
    assert "temperature" not in captured["json"]
    assert captured["json"]["stream"] is True


@pytest.mark.asyncio
async def test_stream_chat_empty_stream_raises_runtime_error():
    client = _make_client()

    async def _empty_sse():
        yield b"data: [DONE]\n\n"

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = _empty_sse()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        with pytest.raises(RuntimeError, match="returned empty output"):
            async for _ in client.stream_chat(
                messages=[{"role": "user", "content": "hi"}],
                model="unloaded-model",
            ):
                pass


@pytest.mark.asyncio
async def test_stream_chat_error_body_raises_runtime_error():
    client = _make_client()

    async def _err_body():
        yield b'{"error": "Model load failed: OOM"}'

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = _err_body()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.post", return_value=mock_cm):
        with pytest.raises(RuntimeError, match="OOM"):
            async for _ in client.stream_chat(
                messages=[{"role": "user", "content": "hi"}],
                model="unloaded-model",
            ):
                pass
