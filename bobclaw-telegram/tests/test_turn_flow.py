"""bobclaw-telegram — /ws/chat turn client tests (fake socket, no live gateway)."""
from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from bobclaw_telegram import gateway_client
from bobclaw_telegram.gateway_client import GatewayError


class _WSMsg:
    def __init__(self, data: dict):
        self.type = aiohttp.WSMsgType.TEXT
        self.data = json.dumps(data)


class _ClosedMsg:
    """A raw CLOSED socket message, as aiohttp delivers on a dropped socket."""

    type = aiohttp.WSMsgType.CLOSED
    data = ""


class _FakeWS:
    """Stands in for aiohttp's ClientWebSocketResponse."""

    def __init__(self, frames: list):
        # dicts become TEXT frames; objects with a .type (e.g. _ClosedMsg)
        # pass through so tests can simulate a mid-stream socket drop.
        self._frames = [f if hasattr(f, "type") else _WSMsg(f) for f in frames]
        self.sent: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send_json(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)


class _FakeWSSession:
    def __init__(self, frames: list[dict]):
        self._frames = frames
        self.ws: _FakeWS | None = None
        self.url: str | None = None
        self.headers: dict | None = None

    def ws_connect(self, url, **kw):
        self.url = url
        self.headers = kw.get("headers") or {}
        self.ws = _FakeWS(self._frames)
        return self.ws

    async def close(self):
        pass


def _run(coro):
    return asyncio.run(coro)


# ── stream_turn ───────────────────────────────────────────────────────────

def test_stream_turn_sends_message_frame_and_yields_chunks():
    session = _FakeWSSession([
        {"type": "chunk", "content": "Hello"},
        {"type": "chunk", "content": " there"},
        {"type": "message_complete", "tokens_in": 1, "tokens_out": 2},
    ])

    async def main():
        chunks = [c async for c in gateway_client.stream_turn(
            "127.0.0.1:7836", "conv-1", "hi", token="t", session=session)]
        return chunks

    chunks = _run(main())
    assert chunks == ["Hello", " there"]
    assert session.ws.sent == [
        {"type": "message", "conversation_id": "conv-1", "content": "hi"}
    ]
    assert session.url == "ws://127.0.0.1:7836/ws/chat"
    assert session.headers["Authorization"] == "Bearer t"


def test_stream_turn_ignores_approval_frames():
    session = _FakeWSSession([
        {"type": "approval_request", "approval_id": "abc", "action": "task_approval"},
        {"type": "chunk", "content": "done"},
        {"type": "message_complete"},
    ])

    async def main():
        return [c async for c in gateway_client.stream_turn(
            "127.0.0.1:7836", "conv-1", "hi", token="t", session=session)]

    assert _run(main()) == ["done"]


def test_stream_turn_on_approval_hook_fires_per_frame():
    frames = [
        {"type": "approval_request", "approval_id": "abc", "action": "task_approval"},
        {"type": "chunk", "content": "done"},
        {"type": "approval_request", "approval_id": "def", "action": "cc_edit"},
        {"type": "message_complete"},
    ]
    session = _FakeWSSession(frames)
    seen: list[dict] = []

    async def on_approval(frame):
        seen.append(frame)

    async def main():
        return [c async for c in gateway_client.stream_turn(
            "127.0.0.1:7836", "conv-1", "hi", token="t", session=session, on_approval=on_approval)]

    assert _run(main()) == ["done"]
    assert [f["approval_id"] for f in seen] == ["abc", "def"]


def test_stream_turn_error_frame_raises_gateway_error():
    session = _FakeWSSession([
        {"type": "error", "message": "Conversation not found", "code": "not_found"},
    ])

    async def main():
        return [c async for c in gateway_client.stream_turn(
            "127.0.0.1:7836", "conv-1", "hi", token="t", session=session)]

    with pytest.raises(GatewayError) as excinfo:
        _run(main())
    assert excinfo.value.code == "not_found"


def test_stream_turn_generation_stopped_ends_cleanly():
    session = _FakeWSSession([
        {"type": "chunk", "content": "partial"},
        {"type": "generation_stopped", "code": "stopped"},
    ])

    async def main():
        return [c async for c in gateway_client.stream_turn(
            "127.0.0.1:7836", "conv-1", "hi", token="t", session=session)]

    assert _run(main()) == ["partial"]


def test_stream_turn_socket_drop_mid_stream_raises():
    """Contract (audit 3A): a socket CLOSED/ERROR mid-stream raises
    GatewayError(code="stream_closed") — a truncated reply must NOT look like
    a clean completion. Clean close after message_complete/generation_stopped
    still returns normally (see the tests above).
    """
    session = _FakeWSSession([
        {"type": "chunk", "content": "partial"},
        _ClosedMsg(),
    ])

    async def main():
        return [c async for c in gateway_client.stream_turn(
            "127.0.0.1:7836", "conv-1", "hi", token="t", session=session)]

    with pytest.raises(GatewayError) as excinfo:
        _run(main())
    assert excinfo.value.code == "stream_closed"


# ── stop_turn ─────────────────────────────────────────────────────────────

def test_stop_turn_sends_stop_generation_with_conversation_id():
    session = _FakeWSSession([{"type": "generation_stopped", "code": "stopped"}])

    stopped = _run(gateway_client.stop_turn("127.0.0.1:7836", "conv-1", token="t", session=session))

    assert stopped is True
    assert session.ws.sent == [
        {"type": "stop_generation", "conversation_id": "conv-1"}
    ]


def test_stop_turn_false_when_nothing_active():
    session = _FakeWSSession([
        {"type": "error", "message": "No active generation to stop",
         "code": "no_active_generation"},
    ])

    stopped = _run(gateway_client.stop_turn("127.0.0.1:7836", "conv-1", token="t", session=session))

    assert stopped is False


# ── create_conversation ───────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _FakeRestSession:
    def __init__(self, resp):
        self._resp = resp
        self.posts: list[dict] = []

    def post(self, url, **kw):
        self.posts.append({"url": url, **kw})
        return self._resp

    async def close(self):
        pass


def test_create_conversation_posts_and_returns_id():
    session = _FakeRestSession(_Resp(201, {"id": "conv-9", "title": "Telegram chat 1"}))

    conv_id = _run(gateway_client.create_conversation(
        "127.0.0.1:7836", title="Telegram chat 1", token="t", session=session))

    assert conv_id == "conv-9"
    post = session.posts[0]
    assert post["url"] == "http://127.0.0.1:7836/conversations"
    assert post["json"] == {"title": "Telegram chat 1"}
    assert post["headers"]["Authorization"] == "Bearer t"


def test_create_conversation_non_2xx_raises():
    session = _FakeRestSession(_Resp(500, {"error": "db down"}))

    with pytest.raises(GatewayError):
        _run(gateway_client.create_conversation("127.0.0.1:7836", token="t", session=session))
