"""bobclaw-telegram — bot handler tests (mocked PTB update + fake gateway).

No live Telegram, no live gateway: handlers duck-type ``update``/``context``,
and the gateway is a fake with the GatewayAdapter interface.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from bobclaw_telegram.bot import (
    GROUP_REFUSAL,
    NOTHING_TO_STOP_REPLY,
    NO_CONVERSATION_REPLY,
    STOPPED_REPLY,
    build_handlers,
)
from bobclaw_telegram.config import NOT_AUTHORIZED_REPLY, Config
from bobclaw_telegram.session_map import SessionMap

ALLOWED = 111
BATCH = 0.05  # short debounce window for tests


# ── fakes ─────────────────────────────────────────────────────────────────

class FakeChat:
    def __init__(self, chat_id=1, chat_type="private"):
        self.id = chat_id
        self.type = chat_type
        self.actions: list[str] = []

    async def send_action(self, action):
        self.actions.append(action)


class FakeSent:
    """A message the bot already sent; records edit-in-place updates."""

    def __init__(self):
        self.edits: list[dict] = []

    async def edit_text(self, text, **kw):
        self.edits.append({"text": text, "kwargs": kw})
        return self


class FakeMessage:
    def __init__(self, chat, text):
        self.chat = chat
        self.text = text
        self.replies: list[dict] = []
        self.sent: list[FakeSent] = []

    async def reply_text(self, text, **kw):
        self.replies.append({"text": text, "kwargs": kw})
        msg = FakeSent()
        self.sent.append(msg)
        return msg


def make_update(update_id, user_id, chat, text):
    return SimpleNamespace(
        update_id=update_id,
        effective_user=SimpleNamespace(id=user_id, username="op"),
        effective_chat=chat,
        message=FakeMessage(chat, text),
    )


class FakeGateway:
    def __init__(self, chunks=("Hello", " there"), error=None, approvals=()):
        self._chunks = chunks
        self._error = error
        self._approvals = approvals
        self.created: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self.stops: list[str] = []

    async def create_conversation(self, title):
        self.created.append(title)
        return "conv-1"

    def stream_turn(self, conversation_id, content, on_approval=None):
        self.turns.append((conversation_id, content))
        return self._gen(on_approval)

    async def _gen(self, on_approval):
        if self._error is not None:
            raise self._error
        for frame in self._approvals:
            if on_approval is not None:
                await on_approval(frame)
        for chunk in self._chunks:
            yield chunk

    async def stop_turn(self, conversation_id):
        self.stops.append(conversation_id)
        return True


def _setup(tmp_path, gateway=None, **kw):
    config = Config(bot_token="t", allowed_users=frozenset({ALLOWED}))
    sessions = SessionMap(tmp_path / ".data" / "sessions.db")
    gateway = gateway or FakeGateway()
    handlers = build_handlers(
        config=config,
        sessions=sessions,
        gateway=gateway,
        batch_delay=BATCH,
        edit_interval=0.0,     # every streamed chunk is "due"
        typing_interval=60.0,  # one typing ping at turn start
    )
    return handlers, sessions, gateway


def _run(coro):
    return asyncio.run(coro)


async def _settle():
    """Let any armed batcher flush tasks fire."""
    await asyncio.sleep(BATCH * 4)


# ── gating ────────────────────────────────────────────────────────────────

def test_non_allowlisted_user_refused_no_turn(tmp_path):
    handlers, sessions, gw = _setup(tmp_path)
    chat = FakeChat()

    async def main():
        update = make_update(1, 999, chat, "hello")
        await handlers.text(update, None)
        await _settle()
        return update

    update = _run(main())
    assert [r["text"] for r in update.message.replies] == [NOT_AUTHORIZED_REPLY]
    assert gw.turns == [] and gw.created == []


def test_group_chat_politely_refused(tmp_path):
    handlers, sessions, gw = _setup(tmp_path)
    chat = FakeChat(chat_type="group")

    async def main():
        update = make_update(1, ALLOWED, chat, "hello")
        await handlers.text(update, None)
        await _settle()
        return update

    update = _run(main())
    assert [r["text"] for r in update.message.replies] == [GROUP_REFUSAL]
    assert gw.turns == [] and gw.created == []


# ── batching ──────────────────────────────────────────────────────────────

def test_two_fast_messages_become_one_turn(tmp_path):
    handlers, sessions, gw = _setup(tmp_path)
    chat = FakeChat()

    async def main():
        await handlers.text(make_update(1, ALLOWED, chat, "hello"), None)
        await handlers.text(make_update(2, ALLOWED, chat, "world"), None)
        await _settle()

    _run(main())
    assert gw.turns == [("conv-1", "hello\nworld")]  # ONE BoB turn, joined
    assert gw.created == ["Telegram chat 1"]


def test_messages_beyond_window_become_two_turns(tmp_path):
    handlers, sessions, gw = _setup(tmp_path)
    chat = FakeChat()

    async def main():
        await handlers.text(make_update(1, ALLOWED, chat, "first"), None)
        await asyncio.sleep(BATCH * 4)  # let the window fire
        await handlers.text(make_update(2, ALLOWED, chat, "second"), None)
        await _settle()

    _run(main())
    assert gw.turns == [("conv-1", "first"), ("conv-1", "second")]
    assert gw.created == ["Telegram chat 1"]  # conversation reused


# ── idempotency ───────────────────────────────────────────────────────────

def test_replayed_update_skipped(tmp_path):
    """Replay after a COMPLETED turn is skipped via the watermark.

    Contract (audit 3A): the update_id watermark advances only AFTER the turn
    dispatches (in the batcher flush), not at receipt — so this test lets the
    first turn complete (watermark → 7) before the replayed update arrives.
    """
    handlers, sessions, gw = _setup(tmp_path)
    chat = FakeChat()

    async def main():
        await handlers.text(make_update(7, ALLOWED, chat, "hello"), None)
        await _settle()  # turn dispatched; watermark advanced to 7
        await handlers.text(make_update(7, ALLOWED, chat, "hello"), None)  # replay
        await _settle()

    _run(main())
    assert gw.turns == [("conv-1", "hello")]  # replay produced no second turn
    assert sessions.last_update_id() == 7


def test_duplicate_update_id_in_flight_batches_together(tmp_path):
    """A duplicate update_id arriving while the first turn is still in-flight
    (same batch window, watermark not yet advanced) is NOT skipped — it
    batches into the same turn. Intended per the audit-3A contract: marking
    at receipt would silently drop messages on a mid-window restart.
    """
    handlers, sessions, gw = _setup(tmp_path)
    chat = FakeChat()

    async def main():
        await handlers.text(make_update(7, ALLOWED, chat, "hello"), None)
        await handlers.text(make_update(7, ALLOWED, chat, "hello again"), None)
        await _settle()

    _run(main())
    assert gw.turns == [("conv-1", "hello\nhello again")]
    assert sessions.last_update_id() == 7


# ── turn delivery ─────────────────────────────────────────────────────────

def test_turn_streams_edits_then_final_markdown(tmp_path):
    handlers, sessions, gw = _setup(tmp_path, FakeGateway(chunks=("Hello", " there")))
    chat = FakeChat()

    async def main():
        update = make_update(1, ALLOWED, chat, "hi")
        await handlers.text(update, None)
        await _settle()
        return update

    update = _run(main())
    streamed = update.message.sent[0]  # initial reply, then edited in place
    assert streamed.edits, "streamed reply should be edited in place"
    assert streamed.edits[-1]["kwargs"].get("parse_mode") == "MarkdownV2"
    assert "Hello there" in streamed.edits[-1]["text"]
    assert "typing" in chat.actions  # typing indicator while the turn ran


def test_turn_error_surfaces_friendly_reply(tmp_path):
    handlers, sessions, gw = _setup(tmp_path, FakeGateway(error=RuntimeError("boom")))
    chat = FakeChat()

    async def main():
        update = make_update(1, ALLOWED, chat, "hi")
        await handlers.text(update, None)
        await _settle()
        return update

    update = _run(main())
    assert any("BoB turn failed" in r["text"] for r in update.message.replies)


# ── /stop ─────────────────────────────────────────────────────────────────

def test_stop_uses_mapped_conversation(tmp_path):
    handlers, sessions, gw = _setup(tmp_path)
    sessions.ensure_conversation(1, "conv-1")
    chat = FakeChat()

    async def main():
        update = make_update(1, ALLOWED, chat, "/stop")
        await handlers.stop(update, None)
        return update

    update = _run(main())
    assert gw.stops == ["conv-1"]
    assert [r["text"] for r in update.message.replies] == [STOPPED_REPLY]


def test_stop_without_conversation(tmp_path):
    handlers, sessions, gw = _setup(tmp_path)
    chat = FakeChat()

    async def main():
        update = make_update(1, ALLOWED, chat, "/stop")
        await handlers.stop(update, None)
        return update

    update = _run(main())
    assert gw.stops == []
    assert [r["text"] for r in update.message.replies] == [NO_CONVERSATION_REPLY]


def test_stop_nothing_active(tmp_path):
    handlers, sessions, gw = _setup(tmp_path)
    sessions.ensure_conversation(1, "conv-1")
    chat = FakeChat()

    class IdleGateway(FakeGateway):
        async def stop_turn(self, conversation_id):
            self.stops.append(conversation_id)
            return False

    gw2 = IdleGateway()
    handlers, sessions, gw2 = _setup(tmp_path, gw2)
    sessions.ensure_conversation(1, "conv-1")

    async def main():
        update = make_update(1, ALLOWED, chat, "/stop")
        await handlers.stop(update, None)
        return update

    update = _run(main())
    assert [r["text"] for r in update.message.replies] == [NOTHING_TO_STOP_REPLY]


def test_stop_gated(tmp_path):
    handlers, sessions, gw = _setup(tmp_path)
    sessions.ensure_conversation(1, "conv-1")
    chat = FakeChat()

    async def main():
        update = make_update(1, 999, chat, "/stop")
        await handlers.stop(update, None)
        return update

    update = _run(main())
    assert gw.stops == []
    assert [r["text"] for r in update.message.replies] == [NOT_AUTHORIZED_REPLY]


# ── rate limiting (Task 3) ──────────────────────────────────────────────────

def test_turn_beyond_rate_limit_refused_no_gateway_call(tmp_path):
    from bobclaw_telegram.ratelimit import RATE_LIMIT_MINUTE_REPLY, RateLimiter

    limiter = RateLimiter(per_minute=1, per_day=100)
    config = Config(bot_token="t", allowed_users=frozenset({ALLOWED}))
    sessions = SessionMap(tmp_path / ".data" / "sessions.db")
    gw = FakeGateway()
    handlers = build_handlers(
        config=config, sessions=sessions, gateway=gw, limiter=limiter,
        batch_delay=BATCH, edit_interval=0.0, typing_interval=60.0,
    )
    chat = FakeChat()

    async def main():
        first = make_update(1, ALLOWED, chat, "one")
        await handlers.text(first, None)
        await _settle()
        second = make_update(2, ALLOWED, chat, "two")
        await handlers.text(second, None)
        await _settle()
        return first, second

    first, second = _run(main())
    assert len(gw.turns) == 1  # only the first turn reached the gateway
    assert [r["text"] for r in second.message.replies] == [RATE_LIMIT_MINUTE_REPLY]


# ── approval park note (Task 3) ─────────────────────────────────────────────

def test_approval_frame_adds_park_note_once(tmp_path):
    from bobclaw_telegram.bot import APPROVAL_PARK_NOTE

    gw = FakeGateway(
        chunks=("ok",),
        approvals=[
            {"type": "approval_request", "approval_id": "a1"},
            {"type": "approval_request", "approval_id": "a2"},
        ],
    )
    handlers, sessions, gw = _setup(tmp_path, gw)
    chat = FakeChat()

    async def main():
        update = make_update(1, ALLOWED, chat, "hi")
        await handlers.text(update, None)
        await _settle()
        return update

    update = _run(main())
    notes = [r["text"] for r in update.message.replies if r["text"] == APPROVAL_PARK_NOTE]
    assert len(notes) == 1  # one note per turn, not per frame
    assert gw.turns == [("conv-1", "hi")]
