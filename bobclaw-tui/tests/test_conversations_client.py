"""BoBClaw TUI — ConversationsClient unit tests (Wave 1B Task 1).

Mocked-aiohttp discipline: a fake session records calls and replays queued responses, so
the REST shapes (list ``{items}``, 201 create, rename route, cursor-paginated messages)
are pinned without a gateway. The pure formatters (``format_conversation_row`` /
``format_history_line``) are tested here too — no I/O, no Textual.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bobclaw_tui.conversations_client import (
    ConversationsClient,
    format_conversation_row,
    format_history_line,
)


def _run(coro):
    return asyncio.run(coro)


class _Resp:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        return json.dumps(self._payload)


class _FakeSession:
    """Replays queued ``_Resp`` objects in order, recording every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []  # (method, url, kwargs)

    def _next(self, method, url, **kw):
        self.calls.append((method, url, kw))
        assert self._responses, f"unexpected call: {method} {url}"
        return self._responses.pop(0)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)

    def post(self, url, **kw):
        return self._next("POST", url, **kw)


def _client(session) -> ConversationsClient:
    return ConversationsClient("127.0.0.1:7836", "tok", session)


# ── list ──
def test_list_conversations_returns_items():
    items = [
        {"id": "c2", "title": "newer", "updated_at": "2026-08-18T10:00:00"},
        {"id": "c1", "title": "older", "updated_at": "2026-08-18T09:00:00"},
    ]
    s = _FakeSession([_Resp(200, {"items": items, "limit": 50, "offset": 0})])
    got = _run(_client(s).list_conversations())
    assert got == items  # server ordering (newest first) preserved
    method, url, kw = s.calls[0]
    assert method == "GET" and url == "http://127.0.0.1:7836/conversations"
    assert kw["headers"]["Authorization"] == "Bearer tok"


def test_list_conversations_raises_on_error():
    s = _FakeSession([_Resp(500, {"error": "boom"})])
    with pytest.raises(RuntimeError, match="500"):
        _run(_client(s).list_conversations())


# ── get (resume probe) ──
def test_get_conversation_returns_row():
    s = _FakeSession([_Resp(200, {"id": "c1", "title": "hello"})])
    assert _run(_client(s).get_conversation("c1")) == {"id": "c1", "title": "hello"}
    assert s.calls[0][1] == "http://127.0.0.1:7836/conversations/c1"


def test_get_conversation_none_on_404():
    s = _FakeSession([_Resp(404, {})])
    assert _run(_client(s).get_conversation("gone")) is None


# ── create ──
def test_create_posts_title_and_returns_row():
    s = _FakeSession([_Resp(201, {"id": "c9", "title": "my chat"})])
    got = _run(_client(s).create("my chat"))
    assert got["id"] == "c9"
    method, url, kw = s.calls[0]
    assert method == "POST" and kw["json"] == {"title": "my chat", "face_id": "assistant"}


def test_create_raises_on_error():
    s = _FakeSession([_Resp(400, {"error": "bad"})])
    with pytest.raises(RuntimeError, match="400"):
        _run(_client(s).create("x"))


# ── rename ──
def test_rename_posts_to_rename_route():
    s = _FakeSession([_Resp(200, {"id": "c1", "title": "renamed"})])
    got = _run(_client(s).rename("c1", "renamed"))
    assert got["title"] == "renamed"
    method, url, kw = s.calls[0]
    assert url == "http://127.0.0.1:7836/conversations/c1/rename"
    assert kw["json"] == {"title": "renamed"}


def test_rename_raises_on_error():
    s = _FakeSession([_Resp(404, {})])
    with pytest.raises(RuntimeError, match="404"):
        _run(_client(s).rename("gone", "x"))


# ── messages (cursor-paginated → oldest-first) ──
def test_get_messages_single_page_oldest_first():
    # the endpoint pages NEWEST-first; the client must flip to render order
    page = {"items": [
        {"id": "m2", "role": "assistant", "content": "answer"},
        {"id": "m1", "role": "user", "content": "question"},
    ], "has_more": False}
    s = _FakeSession([_Resp(200, page)])
    got = _run(_client(s).get_messages("c1"))
    assert [m["id"] for m in got] == ["m1", "m2"]


def test_get_messages_paginates_with_before_cursor():
    page1 = {"items": [
        {"id": "m3", "role": "assistant", "content": "a2"},
        {"id": "m2", "role": "user", "content": "q2"},
    ], "has_more": True}
    page2 = {"items": [
        {"id": "m1", "role": "user", "content": "q1"},
    ], "has_more": False}
    s = _FakeSession([_Resp(200, page1), _Resp(200, page2)])
    got = _run(_client(s).get_messages("c1"))
    assert [m["id"] for m in got] == ["m1", "m2", "m3"]  # full history, oldest-first
    # the second page walked backward via before=<oldest id of page 1>
    assert s.calls[1][2]["params"]["before"] == "m2"


# ── pure formatters ──
def test_format_conversation_row():
    row = format_conversation_row({"title": "my chat", "updated_at": "2026-08-18T10:26:23"})
    assert row == "my chat · 2026-08-18T10:26"  # trimmed to the minute


def test_format_conversation_row_missing_fields():
    assert format_conversation_row({"title": "only title"}) == "only title"
    assert format_conversation_row({}) == "(untitled)"


def test_format_history_line_role_prefixes():
    assert format_history_line({"role": "user", "content": "hi"}) == "you› hi"
    assert format_history_line({"role": "assistant", "content": "yo"}) == "bob› yo"
    # an unknown role passes through rather than dropping the line
    assert format_history_line({"role": "system", "content": "note"}) == "system› note"
