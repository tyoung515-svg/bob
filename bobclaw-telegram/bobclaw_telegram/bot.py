"""bobclaw-telegram — PTB handlers and turn plumbing (phase 3A, Task 2).

PTB-free by design: handlers duck-type ``update``/``context`` so tests drive
them with plain fakes; only ``__main__`` builds the real PTB Application.

Flow: allowlist gate → DMs-only check → update_id idempotency → batching
(~0.6s debounce, rapid messages joined with newline into ONE BoB turn) →
find-or-create conversation → /ws/chat turn via the gateway client →
edit-in-place streamed reply (~1 edit / 0.8s) → final fence-aware chunked
send. Typing indicator runs while the turn runs. ``/stop`` → stop_generation.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from types import SimpleNamespace

from . import gateway_client
from .config import NOT_AUTHORIZED_REPLY, Config
from .ratelimit import RateLimiter
from .render import (
    TELEGRAM_LIMIT,
    EditThrottle,
    chunk_text,
    markdown_rejected,
    md_v2,
)
from .session_map import SessionMap

log = logging.getLogger("bobclaw-telegram")

BATCH_DELAY = 0.6          # debounce window for rapid messages
TYPING_COOLDOWN = 4.0      # Telegram typing action lasts ~5s; re-send under that
TELEGRAM_FACE = "telegram-bob"  # the bot's persona face (tools + BoB identity)
GROUP_REFUSAL = "I only chat in DMs right now — message me directly."
NO_CONVERSATION_REPLY = "No conversation yet — send me a message first."
STOPPED_REPLY = "Stopped."
NOTHING_TO_STOP_REPLY = "Nothing is generating right now."
# Sent (once per turn) when the turn parks on a core approval wait — the
# stream stays open and completes when the approval is decided in the TUI.
APPROVAL_PARK_NOTE = (
    "⏳ This turn is waiting on an approval — decide in the TUI; "
    "it will continue once decided."
)


# ── update helpers ────────────────────────────────────────────────────────

def _user_id(update) -> int | None:
    user = getattr(update, "effective_user", None)
    return user.id if user else None


def _chat(update):
    return getattr(update, "effective_chat", None) or getattr(update.message, "chat", None)


def _is_dm(update) -> bool:
    chat = _chat(update)
    return getattr(chat, "type", None) == "private"


async def _refuse(update) -> None:
    user = getattr(update, "effective_user", None)
    log.warning(
        "refused telegram user id=%r username=%r",
        _user_id(update),
        getattr(user, "username", None),
    )
    if update.message:
        await update.message.reply_text(NOT_AUTHORIZED_REPLY)


# ── batching ──────────────────────────────────────────────────────────────

class MessageBatcher:
    """Debounce rapid messages per chat into ONE turn.

    Each message (re)arms a *delay*-second timer; when it fires without a
    newer message, all buffered texts are joined with newline and flushed
    once. Synchronous ``add`` — the flush runs as an asyncio task.
    """

    def __init__(self, delay: float = BATCH_DELAY) -> None:
        self.delay = delay
        self._pending: dict[int, list[str]] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._callbacks: dict[int, Callable] = {}
        self._update_ids: dict[int, list[int]] = {}

    def add(self, chat_id: int, text: str, on_flush: Callable, update_id=None) -> None:
        self._pending.setdefault(chat_id, []).append(text)
        if update_id is not None:
            self._update_ids.setdefault(chat_id, []).append(update_id)
        self._callbacks[chat_id] = on_flush
        task = self._tasks.get(chat_id)
        if task is not None and not task.done():
            task.cancel()
        self._tasks[chat_id] = asyncio.ensure_future(self._flush_later(chat_id))

    async def _flush_later(self, chat_id: int) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return  # re-armed by a newer message
        texts = self._pending.pop(chat_id, [])
        on_flush = self._callbacks.pop(chat_id, None)
        update_ids = self._update_ids.pop(chat_id, [])
        self._tasks.pop(chat_id, None)
        if texts and on_flush is not None:
            await on_flush(chat_id, "\n".join(texts), update_ids)


# ── markdown send with plain-text fallback ────────────────────────────────

_TURN_LOCKS: dict[int, asyncio.Lock] = {}


def _turn_lock(chat_id: int) -> asyncio.Lock:
    """One in-flight turn per chat — a second flush while a turn is parked (e.g. on
    a core approval wait) waits its turn instead of racing the stream (audit 3A)."""
    return _TURN_LOCKS.setdefault(chat_id, asyncio.Lock())

async def _reply_md(message, text: str):
    """Reply with degraded MarkdownV2; any BadRequest → plain text resend."""
    try:
        return await message.reply_text(md_v2(text), parse_mode="MarkdownV2")
    except Exception as exc:
        if not markdown_rejected(exc):
            raise
        log.info("markdown rejected by Telegram; resending as plain text")
        return await message.reply_text(text)


async def _edit_md(sent, text: str):
    try:
        return await sent.edit_text(md_v2(text), parse_mode="MarkdownV2")
    except Exception as exc:
        if not markdown_rejected(exc):
            raise
        if _is_not_modified(exc):
            return sent  # final render identical to the last streamed edit
        log.info("markdown rejected by Telegram; editing as plain text")
        try:
            return await sent.edit_text(text)
        except Exception as plain_exc:
            if markdown_rejected(plain_exc) and _is_not_modified(plain_exc):
                return sent
            raise


def _is_not_modified(exc: BaseException) -> bool:
    return "not modified" in str(exc).lower()


# ── typing indicator ──────────────────────────────────────────────────────

async def _typing_loop(update, stop: asyncio.Event, interval: float) -> None:
    """Send the typing action immediately, then at most once per *interval*."""
    chat = _chat(update)
    while not stop.is_set():
        try:
            await chat.send_action("typing")
        except Exception:  # noqa: BLE001 — best-effort cosmetic signal
            log.debug("typing indicator failed", exc_info=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


# ── handlers ──────────────────────────────────────────────────────────────

def build_handlers(
    *,
    config: Config,
    sessions: SessionMap,
    gateway,
    limiter: RateLimiter | None = None,
    batch_delay: float = BATCH_DELAY,
    edit_interval: float = 0.8,
    typing_interval: float = TYPING_COOLDOWN,
    clock: Callable[[], float] = time.monotonic,
) -> SimpleNamespace:
    """Build the gated Task-2/Task-3 handlers.

    *gateway* is any object with ``create_conversation(title) -> str``,
    ``stream_turn(conversation_id, content, on_approval=None) ->
    AsyncIterator[str]``, and ``stop_turn(conversation_id) -> bool`` (see
    ``GatewayAdapter``); tests inject a fake. *limiter* is the per-chat turn
    rate limiter (Task 3); defaults to the standard 10/min + 200/day caps.
    """
    batcher = MessageBatcher(delay=batch_delay)
    limiter = limiter or RateLimiter()

    async def ping(update, context) -> None:
        if not config.is_allowed(_user_id(update)):
            await _refuse(update)
            return
        await update.message.reply_text("pong")

    async def stop(update, context) -> None:
        if not config.is_allowed(_user_id(update)):
            await _refuse(update)
            return
        if not _is_dm(update):
            await update.message.reply_text(GROUP_REFUSAL)
            return
        chat_id = _chat(update).id
        conversation_id = sessions.conversation_for(chat_id)
        if conversation_id is None:
            await update.message.reply_text(NO_CONVERSATION_REPLY)
            return
        stopped = await gateway.stop_turn(conversation_id)
        await update.message.reply_text(STOPPED_REPLY if stopped else NOTHING_TO_STOP_REPLY)

    async def text(update, context) -> None:
        if not config.is_allowed(_user_id(update)):
            await _refuse(update)
            return
        if not _is_dm(update):
            log.info("refused group message from user id=%r", _user_id(update))
            await update.message.reply_text(GROUP_REFUSAL)
            return
        update_id = getattr(update, "update_id", None)
        if update_id is not None and sessions.is_replay(update_id):
            log.info("skipping replayed update_id=%r", update_id)
            return
        # NOTE: the watermark is advanced only after the turn actually reaches the
        # gateway (in flush below) — marking at receipt would silently DROP the
        # message on a mid-window restart, which is worse than replaying it
        # (audit 3A task-2).
        body = (getattr(update.message, "text", None) or "").strip()
        if not body:
            return

        async def flush(chat_id: int, joined: str, update_ids: list, _update=update) -> None:
            async with _turn_lock(chat_id):  # one in-flight turn per chat (audit 3A)
                await _run_turn(_update, chat_id, joined)
            for uid in update_ids:
                sessions.mark_processed(uid)

        batcher.add(_chat(update).id, body, flush, update_id=update_id)

    async def _run_turn(update, chat_id: int, content: str) -> None:
        refusal = limiter.check(chat_id)
        if refusal is not None:
            log.info("rate-limited chat %s", chat_id)
            await update.message.reply_text(refusal)
            return
        conversation_id = sessions.conversation_for(chat_id)
        if conversation_id is None:
            conversation_id = sessions.ensure_conversation(
                chat_id, await gateway.create_conversation(f"Telegram chat {chat_id}")
            )
            log.info("chat %s -> new conversation %s", chat_id, conversation_id)

        approval_note_sent = False

        async def _note_approval(frame: dict) -> None:
            # The turn parked on an approval wait; the stream stays open and
            # completes when the approval is decided elsewhere (the TUI).
            nonlocal approval_note_sent
            if approval_note_sent:
                return
            approval_note_sent = True
            await update.message.reply_text(APPROVAL_PARK_NOTE)

        stop_typing = asyncio.Event()
        typing_task = asyncio.ensure_future(
            _typing_loop(update, stop_typing, typing_interval)
        )
        await asyncio.sleep(0)  # let the first typing ping fire before streaming
        acc = ""
        streamed_msg = None
        throttle = EditThrottle(edit_interval)
        try:
            async for chunk in gateway.stream_turn(
                conversation_id, content, on_approval=_note_approval
            ):
                acc += chunk
                # Edit-in-place only while the reply fits one message; the
                # final send chunk_text()s anything longer.
                if len(acc) <= TELEGRAM_LIMIT and throttle.due(clock()):
                    if streamed_msg is None:
                        streamed_msg = await _reply_md(update.message, acc)
                    else:
                        await _edit_md(streamed_msg, acc)
        except Exception as exc:  # noqa: BLE001 — surface to the operator, keep bot alive
            log.exception("turn failed for chat %s", chat_id)
            if not acc:
                acc = f"⚠️ BoB turn failed: {exc}"
        finally:
            stop_typing.set()
            await typing_task

        await _deliver_final(update.message, streamed_msg, acc)

    return SimpleNamespace(ping=ping, stop=stop, text=text, batcher=batcher)


async def _deliver_final(message, streamed_msg, text: str) -> None:
    """Final render: fence-aware chunks; first replaces the streamed message."""
    chunks = chunk_text(text) if text else ["(no response)"]
    first, rest = chunks[0], chunks[1:]
    if streamed_msg is not None:
        await _edit_md(streamed_msg, first)
    else:
        await _reply_md(message, first)
    for extra in rest:
        await _reply_md(message, extra)


class GatewayAdapter:
    """Thin adapter over gateway_client — token fetch/rotation stays there."""

    def __init__(self, gateway: str) -> None:
        self._gateway = gateway

    async def create_conversation(self, title: str) -> str:
        return await gateway_client.create_conversation(
            self._gateway, title=title, face_id=TELEGRAM_FACE
        )

    def stream_turn(
        self, conversation_id: str, content: str, on_approval=None
    ) -> AsyncIterator[str]:
        return gateway_client.stream_turn(
            self._gateway, conversation_id, content, on_approval=on_approval,
            face_id=TELEGRAM_FACE,
        )

    async def stop_turn(self, conversation_id: str) -> bool:
        return await gateway_client.stop_turn(self._gateway, conversation_id)
