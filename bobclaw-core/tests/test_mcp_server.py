"""Tests for the Neck Beard MODE MCP server (provider side).

Everything is exercised with injected fakes — no network. Covers: faces-claim
decoding, the faces/council allow-list, conditional tool registration, the WS
stream consumer, conversation auto-create, per-face routing, and local rejection
of an out-of-allowlist face (no upstream call).
"""
from __future__ import annotations

import asyncio
import json

import aiohttp
import jwt
import pytest

from core.mcp.server import (
    COUNCIL_FACE,
    DEFAULT_GATEWAY,
    MCPConfig,
    _chat_turn,
    _consume_stream,
    _create_conversation,
    _decode_faces,
    build_server,
    chat_with_face_impl,
    load_config,
    run_council_impl,
)


def _cfg(faces=None, gateway="http://127.0.0.1:7826") -> MCPConfig:
    return MCPConfig(token="t.t.t", gateway=gateway, faces=faces or [])


def _tok(**claims) -> str:
    # Secret is irrelevant — the server decodes faces WITHOUT verifying — but use
    # a 32+ char one to avoid pyjwt's short-key warning in the suite.
    return jwt.encode(claims, "0123456789abcdef0123456789abcdef", algorithm="HS256")


# ── fakes ────────────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeMsg:
    def __init__(self, type_, data):
        self.type = type_
        self.data = data


class _FakeWS:
    def __init__(self, messages):
        self._messages = messages
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)

    def __aiter__(self):
        self._it = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    """Minimal stand-in for aiohttp.ClientSession: records post/ws calls."""

    def __init__(self, conv_id="conv-1", ws=None, post_status=200):
        self._conv_id = conv_id
        self._ws = ws or _FakeWS([])
        self._post_status = post_status
        self.post_calls = []
        self.ws_calls = []
        self.closed = False

    def post(self, url, json=None, headers=None):
        self.post_calls.append((url, json, headers))
        return _FakeResp(self._post_status, {"id": self._conv_id})

    def ws_connect(self, url, headers=None):
        self.ws_calls.append((url, headers))
        return self._ws

    async def close(self):
        self.closed = True


class _MultiWSSession(_FakeSession):
    """A session that hands out a fresh fake WS per ws_connect call."""

    def __init__(self, conv_id, wss):
        super().__init__(conv_id=conv_id, ws=wss[0])
        self.wss = wss
        self._i = 0

    def ws_connect(self, url, headers=None):
        self.ws_calls.append((url, headers))
        ws = self.wss[self._i]
        self._i += 1
        return ws


def _text_frames(*evts):
    return [_FakeMsg(aiohttp.WSMsgType.TEXT, json.dumps(e)) for e in evts]


# ── faces decode + allow-list ─────────────────────────────────────────────────


def test_decode_faces_reads_claim():
    assert _decode_faces(_tok(faces=["assistant", "council-max"])) == [
        "assistant",
        "council-max",
    ]


def test_decode_faces_bad_token_is_empty():
    assert _decode_faces("not.a.jwt") == []
    assert _decode_faces(_tok(faces=[1, "ok", None])) == ["ok"]  # non-strings dropped


def test_face_allowed_empty_is_unrestricted():
    assert _cfg(faces=[]).face_allowed("anything")


def test_face_allowed_nonempty_is_strict():
    cfg = _cfg(faces=["assistant"])
    assert cfg.face_allowed("assistant")
    assert not cfg.face_allowed("reviewer")


def test_council_allowed_requires_council_max_face():
    # run_council proxies COUNCIL_FACE (council-max), so the gate is consistent
    # with the face actually invoked — not a loose "startswith council" match.
    assert _cfg(faces=[]).council_allowed  # unrestricted
    assert _cfg(faces=["council-max"]).council_allowed
    assert _cfg(faces=["assistant", "council-max"]).council_allowed
    assert not _cfg(faces=["council-lite"]).council_allowed  # not council-max
    assert not _cfg(faces=["assistant"]).council_allowed


# ── tool registration ─────────────────────────────────────────────────────────


def _tool_names(cfg):
    return {t.name for t in build_server(cfg)._tool_manager.list_tools()}


def test_registers_both_tools_when_council_allowed():
    assert _tool_names(_cfg(faces=[])) == {"chat_with_face", "run_council"}
    assert _tool_names(_cfg(faces=["assistant", "council-max"])) == {
        "chat_with_face",
        "run_council",
    }


def test_omits_council_when_not_allowed():
    assert _tool_names(_cfg(faces=["assistant"])) == {"chat_with_face"}


# ── stream consumer ────────────────────────────────────────────────────────────


def test_consume_stream_accumulates_until_complete():
    ws = _FakeWS(
        _text_frames(
            {"type": "chunk", "content": "Hello"},
            {"type": "chunk", "content": " world"},
            {"type": "message_complete", "message_id": "a1"},
            {"type": "chunk", "content": " IGNORED-after-complete"},
        )
    )
    assert asyncio.run(_consume_stream(ws)) == "Hello world"


def test_consume_stream_surfaces_error_frame():
    ws = _FakeWS(_text_frames({"type": "error", "code": "state_error", "message": "boom"}))
    out = asyncio.run(_consume_stream(ws))
    assert out == "[error:state_error] boom"


def test_consume_stream_returns_partial_on_generation_stopped():
    ws = _FakeWS(
        _text_frames(
            {"type": "chunk", "content": "par"},
            {"type": "chunk", "content": "tial"},
            {"type": "generation_stopped", "code": "superseded"},
        )
    )
    assert asyncio.run(_consume_stream(ws)) == "partial"


# ── conversation create + chat turn ────────────────────────────────────────────


def test_create_conversation_posts_and_returns_id():
    sess = _FakeSession(conv_id="conv-XYZ")
    cid = asyncio.run(_create_conversation(sess, _cfg(), "title"))
    assert cid == "conv-XYZ"
    url, body, headers = sess.post_calls[0]
    assert url.endswith("/conversations")
    assert body == {"title": "title"}
    assert headers["Authorization"] == "Bearer t.t.t"


def test_create_conversation_raises_on_error_status():
    sess = _FakeSession(post_status=403)
    with pytest.raises(RuntimeError):
        asyncio.run(_create_conversation(sess, _cfg(), "title"))


def test_chat_turn_creates_conversation_when_absent_and_sends_face():
    ws = _FakeWS(_text_frames({"type": "chunk", "content": "hi"}, {"type": "message_complete"}))
    sess = _FakeSession(conv_id="fresh-conv", ws=ws)
    out = asyncio.run(_chat_turn(_cfg(), "hello", "assistant", "", session=sess))
    assert out == "hi"
    assert len(sess.post_calls) == 1  # created a conversation
    assert ws.sent[0] == {
        "type": "message",
        "conversation_id": "fresh-conv",
        "content": "hello",
        "face_id": "assistant",
    }


def test_chat_turn_reuses_given_conversation_id_no_create():
    ws = _FakeWS(_text_frames({"type": "message_complete"}))
    sess = _FakeSession(ws=ws)
    asyncio.run(_chat_turn(_cfg(), "hi", "assistant", "existing-conv", session=sess))
    assert sess.post_calls == []  # no conversation created
    assert ws.sent[0]["conversation_id"] == "existing-conv"


def test_chat_turn_reuses_default_conversation_across_calls():
    """Repeated empty-id calls continue ONE thread (created once, then reused)."""
    cfg = _cfg()
    sess = _MultiWSSession(
        conv_id="conv-A",
        wss=[
            _FakeWS(_text_frames({"type": "message_complete"})),
            _FakeWS(_text_frames({"type": "message_complete"})),
        ],
    )
    asyncio.run(_chat_turn(cfg, "one", "assistant", "", session=sess))
    asyncio.run(_chat_turn(cfg, "two", "assistant", "", session=sess))
    assert len(sess.post_calls) == 1  # conversation created once, reused after
    assert {ws.sent[0]["conversation_id"] for ws in sess.wss} == {"conv-A"}


def test_chat_turn_surfaces_403_as_forbidden_in_band():
    """The Phase-1 default-deny (403 on create) surfaces as a clean [error:...],
    not a raised exception."""
    sess = _FakeSession(post_status=403)
    out = asyncio.run(_chat_turn(_cfg(), "hi", "assistant", "", session=sess))
    assert out.startswith("[error:forbidden]")


def test_chat_turn_surfaces_connection_error_as_upstream():
    class _BoomSession(_FakeSession):
        def post(self, *a, **k):
            raise aiohttp.ClientConnectionError("gateway down")

    out = asyncio.run(_chat_turn(_cfg(), "hi", "assistant", "", session=_BoomSession()))
    assert out.startswith("[error:upstream]")


def test_chat_turn_times_out_on_never_completing_stream():
    class _HangWS(_FakeWS):
        async def __anext__(self):
            await asyncio.sleep(0.02)
            return _FakeMsg(aiohttp.WSMsgType.TEXT, json.dumps({"type": "chunk", "content": "x"}))

    cfg = _cfg()
    cfg.turn_timeout = 0.15  # tiny ceiling
    sess = _FakeSession(ws=_HangWS([]))
    out = asyncio.run(_chat_turn(cfg, "hi", "assistant", "existing", session=sess))
    assert out == "[error:timeout] gateway did not complete the turn in time"


def test_consume_stream_marks_partial_on_abnormal_close():
    ws = _FakeWS(
        _text_frames({"type": "chunk", "content": "half"})
        + [_FakeMsg(aiohttp.WSMsgType.CLOSED, None)]
    )
    out = asyncio.run(_consume_stream(ws))
    assert "half" in out and "[error:connection_closed]" in out


def test_consume_stream_skips_malformed_frame():
    ws = _FakeWS(
        [_FakeMsg(aiohttp.WSMsgType.TEXT, "{not json")]
        + _text_frames({"type": "chunk", "content": "ok"}, {"type": "message_complete"})
    )
    assert asyncio.run(_consume_stream(ws)) == "ok"


# ── tool impls ─────────────────────────────────────────────────────────────────


def test_run_council_impl_sends_council_face():
    ws = _FakeWS(_text_frames({"type": "chunk", "content": "verdict"}, {"type": "message_complete"}))
    sess = _FakeSession(ws=ws)
    out = asyncio.run(run_council_impl(_cfg(), "deliberate this", "", session=sess))
    assert out == "verdict"
    assert ws.sent[0]["face_id"] == COUNCIL_FACE


def test_chat_with_face_rejects_disallowed_face_without_upstream():
    sess = _FakeSession()
    out = asyncio.run(
        chat_with_face_impl(_cfg(faces=["assistant"]), "hi", "reviewer", "", session=sess)
    )
    assert out.startswith("[error:forbidden]")
    assert sess.post_calls == [] and sess.ws_calls == []  # never reached the gateway


def test_run_council_impl_blocked_when_not_allowed():
    sess = _FakeSession()
    out = asyncio.run(run_council_impl(_cfg(faces=["assistant"]), "x", "", session=sess))
    assert out.startswith("[error:forbidden]")
    assert sess.ws_calls == []


# ── config loading ─────────────────────────────────────────────────────────────


def test_load_config_requires_token():
    with pytest.raises(SystemExit):
        load_config({})


def test_load_config_defaults_gateway_and_reads_faces():
    cfg = load_config({"BOBCLAW_AGENT_TOKEN": _tok(faces=["assistant"])})
    assert cfg.gateway == DEFAULT_GATEWAY
    assert cfg.faces == ["assistant"]


def test_load_config_honors_gateway_override():
    cfg = load_config(
        {"BOBCLAW_AGENT_TOKEN": _tok(), "BOBCLAW_GATEWAY": "http://127.0.0.1:9999/"}
    )
    assert cfg.gateway == "http://127.0.0.1:9999"  # trailing slash stripped
