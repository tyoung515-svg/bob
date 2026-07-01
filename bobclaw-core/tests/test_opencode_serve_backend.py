"""
BoBClaw Core — Unit tests for OpenCodeServeClient

All network I/O is mocked; no real OpenCode serve required.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.backends.opencode_serve import OpenCodeServeClient


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_client(host: str = "localhost", port: int = 7900) -> OpenCodeServeClient:
    return OpenCodeServeClient(host=host, port=port)


# ─── Construction ─────────────────────────────────────────────────────────────

def test_base_url_composed_from_host_port():
    client = _make_client("myhost", 1234)
    assert client.base_url == "http://myhost:1234"


# ─── health_check ─────────────────────────────────────────────────────────────

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
async def test_health_check_returns_false_on_non_200():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.status = 503
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("aiohttp.ClientSession.get", return_value=mock_cm):
        assert await client.health_check() is False


@pytest.mark.asyncio
async def test_health_check_returns_false_on_exception():
    client = _make_client()
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(side_effect=OSError("connection refused"))

    with patch("aiohttp.ClientSession.get", return_value=mock_cm):
        assert await client.health_check() is False


# ─── create_session ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_session_posts_and_returns_session_id():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"session_id": "sess-42"})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers=None):
        captured["url"] = url
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        sid = await client.create_session("/tmp/ws")

    assert sid == "sess-42"
    assert captured["url"].endswith("/session")
    assert captured["json"]["workspace_dir"] == "/tmp/ws"


# ─── prompt ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_prompt_posts_message_and_returns_text():
    client = _make_client()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = AsyncMock(return_value={"text": "hello back"})
    mock_cm = AsyncMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_cm.__aexit__ = AsyncMock(return_value=False)

    captured = {}

    def fake_post(url, *, json, headers=None):
        captured["url"] = url
        captured["json"] = json
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        text = await client.prompt("sess-42", "hello")

    assert text == "hello back"
    assert captured["url"].endswith("/session/sess-42/message")
    assert captured["json"]["text"] == "hello"


# ─── chat adapter ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chat_creates_prompts_deletes_session():
    client = _make_client()
    calls = []

    def fake_post(url, *, json, headers=None):
        calls.append(url)
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        if "/session/" in url and "/message" in url:
            mock_resp.json = AsyncMock(return_value={"text": "done"})
        else:
            mock_resp.json = AsyncMock(return_value={"session_id": "sess-99"})
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        return mock_cm

    def fake_delete(url, *, headers=None):
        mock_resp = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        with patch("aiohttp.ClientSession.delete", side_effect=fake_delete) as mock_del:
            result = await client.chat(
                messages=[
                    {"role": "system", "content": "You are a tester."},
                    {"role": "user", "content": "hi"},
                ],
                workspace_dir="/tmp/ws",
            )

    assert result == "done"
    assert any("/session" in c and "/message" not in c for c in calls)
    assert any("/message" in c for c in calls)
    assert mock_del.call_count == 1


@pytest.mark.asyncio
async def test_chat_deletes_session_even_on_error():
    client = _make_client()

    def fake_post(url, *, json, headers=None):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value={"session_id": "sess-1"})
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        return mock_cm

    def fake_delete(url, *, headers=None):
        mock_resp = MagicMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_cm.__aexit__ = AsyncMock(return_value=False)
        return mock_cm

    with patch("aiohttp.ClientSession.post", side_effect=fake_post):
        with patch("aiohttp.ClientSession.delete", side_effect=fake_delete) as mock_del:
            with patch.object(
                client, "prompt", side_effect=RuntimeError("boom")
            ):
                with pytest.raises(RuntimeError, match="boom"):
                    await client.chat(
                        messages=[{"role": "user", "content": "hi"}],
                    )

    assert mock_del.call_count == 1


# ─── timeout enforcement ──────────────────────────────────────────────────────

def test_chat_uses_config_timeout():
    client = _make_client()
    assert client._timeout is not None
    assert client._timeout.total == 300
