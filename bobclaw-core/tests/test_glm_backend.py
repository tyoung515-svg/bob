"""
BoBClaw Core — Unit tests for GLMClient (glm-5.2 / Z.AI)

All network I/O is mocked; no live API calls.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.glm import GLMClient, GLMUnavailableError


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client(api_key: str = "dummy-key", base_url: str | None = None) -> GLMClient:
    return GLMClient(api_key=api_key, base_url=base_url)


# ─── Construction ─────────────────────────────────────────────────────────────

def test_default_base_url_is_coding_paas_v4():
    # Default is the GLM Coding Plan surface (coding/paas/v4), NOT the PAYG balance
    # surface (paas/v4, which 429s code 1113 when the PAYG balance is empty).
    client = _make_client()
    assert client.base_url == "https://api.z.ai/api/coding/paas/v4"


def test_custom_base_url_override():
    client = _make_client(base_url="https://custom.z.ai/api/paas/v4")
    assert client.base_url == "https://custom.z.ai/api/paas/v4"


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


@pytest.mark.asyncio
async def test_health_check_returns_true_on_404_fallback():
    """Z.AI's paas/v4 surface does not always expose /models; 404 falls back to key presence."""
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_cm):
        assert await client.health_check() is True


# ─── chat ─────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_posts_to_coding_paas_v4_chat_completions_with_bearer():
    client = _make_client(api_key="sk-test")
    fake_response = {
        "choices": [{"message": {"content": "Hello from GLM-5.2"}}]
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
        result = await client.chat(messages=[{"role": "user", "content": "hi"}], model="glm-5.2")

    assert result == fake_response
    assert captured["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"
    assert captured["json"]["model"] == "glm-5.2"
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
            model="glm-5.2",
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
            model="glm-5.2",
            stop="",
        )

    assert captured["json"]["stop"] == ""


@pytest.mark.asyncio
async def test_chat_passes_tools_kwarg_through():
    """Tools must flow through to the chat endpoint so the native tool loop can use GLM."""
    client = _make_client(api_key="sk-test")
    fake_tools = [{"type": "function", "function": {"name": "get_server_time"}}]
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
            model="glm-5.2",
            tools=fake_tools,
        )

    assert captured["json"]["tools"] == fake_tools


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


# ─── P0: 429 classification (balance vs transient) ───────────────────────────

def _resp_cm(mock_resp):
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=mock_resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


@pytest.mark.asyncio
async def test_chat_balance_429_raises_glm_unavailable_no_retry():
    """A 429 carrying the Z.AI balance marker (code 1113) raises immediately — retrying a
    balance-exhausted account is futile."""
    client = _make_client(api_key="sk-test")
    mock_resp = MagicMock()
    mock_resp.status = 429
    mock_resp.headers = {}
    mock_resp.text = AsyncMock(
        return_value='{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}'
    )
    posts = {"n": 0}

    def fake_post(url, *, json, headers):
        posts["n"] += 1
        return _resp_cm(mock_resp)

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        with pytest.raises(GLMUnavailableError) as ei:
            await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert "balance" in str(ei.value).lower()
    assert posts["n"] == 1                       # no retry on a balance error


@pytest.mark.asyncio
async def test_chat_transient_5xx_retries_then_raises(monkeypatch):
    """A transient 5xx is retried with backoff, then surfaces as GLMUnavailableError."""
    import core.backends.glm as glm_mod

    monkeypatch.setattr(glm_mod.asyncio, "sleep", AsyncMock())  # no real backoff wait
    client = _make_client(api_key="sk-test")
    mock_resp = MagicMock()
    mock_resp.status = 503
    mock_resp.headers = {}
    mock_resp.text = AsyncMock(return_value="upstream overloaded")
    posts = {"n": 0}

    def fake_post(url, *, json, headers):
        posts["n"] += 1
        return _resp_cm(mock_resp)

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        with pytest.raises(GLMUnavailableError):
            await client.chat(messages=[{"role": "user", "content": "hi"}])
    assert posts["n"] == glm_mod._MAX_RETRIES + 1    # 1 + retries attempts
