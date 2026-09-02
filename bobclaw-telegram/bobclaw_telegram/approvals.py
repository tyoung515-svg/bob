"""bobclaw-telegram — approvals notify-only relay (phase 3A, Task 3).

Polls ``GET /approvals?status=pending`` and relays NEW pending approvals to
each allowlisted Telegram DM. Notify-only: no inline buttons, no remote
decide — all decisions stay in the TUI (the gateway ``/ui`` is gone, so the
TUI is the human-reachable approvals surface).

The diffing + formatting at the top is pure; :class:`ApprovalNotifier` is a
fail-open poll loop over injected ``fetch``/``send`` callables so tests never
touch the network. ``send`` targets the allowlisted *user ids* — Telegram DM
chat ids equal the user id, so no separate chat mapping is needed.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("bobclaw-telegram")

POLL_INTERVAL_S = 15.0
DECIDE_NOTE = "decide in the TUI"


def _summary(details, *, max_len: int = 60) -> str:
    """One-line ``k=v`` summary of an approval's ``details`` dict — mirrors
    ``bobclaw_tui/panels.py::_details_summary`` (first few pairs, truncated)."""
    if not isinstance(details, dict) or not details:
        return ""
    parts = [f"{k}={str(v)[:24]}" for k, v in list(details.items())[:3]]
    summary = ", ".join(parts)
    if len(details) > 3:
        summary += ", …"
    return summary[:max_len]


def notify_text(item: dict) -> str:
    """The relay message for one pending approval.

    "⏳ Approval pending: <kind> — <summary> — decide in the TUI"; the summary
    segment is omitted when the approval carries no details.
    """
    kind = str(item.get("action_type") or "?")
    text = f"⏳ Approval pending: {kind}"
    summary = _summary(item.get("details"))
    if summary:
        text += f" — {summary}"
    return f"{text} — {DECIDE_NOTE}"


def new_pending(items, seen_ids: set[str]) -> list[dict]:
    """Items whose id is not in *seen_ids* — malformed entries (no id) skipped."""
    return [
        i for i in items
        if isinstance(i, dict) and i.get("id") is not None
        and str(i["id"]) not in seen_ids
    ]


class ApprovalNotifier:
    """Diff-based relay: poll pending approvals, notify only on NEW ids.

    ``fetch``: ``() -> Awaitable[list[dict]]`` returning the pending items
    (the ``items`` list from ``GET /approvals?status=pending``).
    ``send``: ``(chat_id, text) -> Awaitable[None]`` delivering one message.

    Fail-open: a failed poll or a failed send logs and moves on — approvals
    notification must never take the bot down. Seen state is in-memory: on
    restart, everything still pending is relayed once (deliberate — the
    operator re-learns what's outstanding).
    """

    def __init__(
        self,
        fetch: Callable[[], Awaitable[list]],
        send: Callable[[int, str], Awaitable[None]],
        chat_ids: list[int],
        *,
        interval: float = POLL_INTERVAL_S,
    ) -> None:
        self._fetch = fetch
        self._send = send
        self._chat_ids = list(chat_ids)
        self._interval = interval
        self._seen: set[str] = set()

    async def poll_once(self) -> list[dict]:
        """One poll: relay each newly-pending approval to every chat. Returns
        the new items (empty on fetch failure or nothing new)."""
        try:
            items = await self._fetch()
        except Exception:  # noqa: BLE001 — a poll never kills the bot
            log.warning("approvals poll failed", exc_info=True)
            return []
        items = [i for i in items if isinstance(i, dict) and i.get("id") is not None]
        new = new_pending(items, self._seen)
        self._seen = {str(i["id"]) for i in items}
        for item in new:
            text = notify_text(item)
            for chat_id in self._chat_ids:
                try:
                    await self._send(chat_id, text)
                except Exception:  # noqa: BLE001 — one bad send doesn't stop the rest
                    log.warning("approval relay to chat %s failed", chat_id, exc_info=True)
        return new

    async def loop(self, stop: asyncio.Event) -> None:
        """Poll every *interval* seconds until *stop* is set, promptly."""
        while not stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval)
            except asyncio.TimeoutError:
                pass
