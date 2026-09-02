"""
BoBClaw Core — DeepSeek thinking-disabled default (Option A)

deepseek-v4-flash is thinking-by-default; raw reasoning once leaked into a
visible reply. The chat path now sends thinking={"type": "disabled"} unless
the caller passes an explicit thinking kwarg. All network I/O is mocked.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.deepseek import DeepSeekClient


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client() -> DeepSeekClient:
    return DeepSeekClient(api_key="sk-test", base_url="https://api.deepseek.test")


def _post_capture(captured: dict, mock_cm):
    def fake_post(url, *, json, headers):
        captured["json"] = json
        return mock_cm
    return fake_post


def _chat_cm() -> AsyncMock:
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"choices": [{"message": {"content": ""}}]})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


def _stream_cm(sse_lines: list[bytes]) -> AsyncMock:
    async def _async_iter(lines):
        for line in lines:
            yield line

    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.content = _async_iter(sse_lines)
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


# ─── thinking disabled by default ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_payload_thinking_disabled_by_default():
    captured = {}
    with patch("aiohttp.ClientSession.post", side_effect=_post_capture(captured, _chat_cm())):
        await _make_client().chat(messages=[{"role": "user", "content": "hi"}])

    assert captured["json"]["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_stream_chat_payload_thinking_disabled_by_default():
    captured = {}
    cm = _stream_cm([b"data: [DONE]\n\n"])
    with patch("aiohttp.ClientSession.post", side_effect=_post_capture(captured, cm)):
        async for _ in _make_client().stream_chat(messages=[{"role": "user", "content": "hi"}]):
            pass

    assert captured["json"]["thinking"] == {"type": "disabled"}


# ─── explicit caller kwarg wins ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_explicit_thinking_kwarg_overrides_default():
    captured = {}
    with patch("aiohttp.ClientSession.post", side_effect=_post_capture(captured, _chat_cm())):
        await _make_client().chat(
            messages=[{"role": "user", "content": "hi"}],
            thinking={"type": "enabled"},
        )

    assert captured["json"]["thinking"] == {"type": "enabled"}


@pytest.mark.asyncio
async def test_stream_chat_explicit_thinking_kwarg_overrides_default():
    captured = {}
    cm = _stream_cm([b"data: [DONE]\n\n"])
    with patch("aiohttp.ClientSession.post", side_effect=_post_capture(captured, cm)):
        async for _ in _make_client().stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            thinking={"type": "enabled"},
        ):
            pass

    assert captured["json"]["thinking"] == {"type": "enabled"}


# ─── regression: reasoning_content deltas are never surfaced ──────────────────

@pytest.mark.asyncio
async def test_stream_chat_reasoning_content_delta_yields_nothing():
    sse_lines = [
        b'data: {"choices":[{"delta":{"reasoning_content":"let me think","content":""}}]}\n\n',
        b'data: {"choices":[{"delta":{"reasoning_content":"still thinking"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"answer"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]

    with patch("aiohttp.ClientSession.post", return_value=_stream_cm(sse_lines)):
        chunks = []
        async for chunk in _make_client().stream_chat(messages=[{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    assert chunks == ["answer"]
