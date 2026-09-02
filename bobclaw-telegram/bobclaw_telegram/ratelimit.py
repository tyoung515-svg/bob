"""bobclaw-telegram — per-chat turn rate limiting (phase 3A, Task 3).

A pure, injectable-clock limiter: a sliding window of N turns per minute per
chat plus a UTC-day cap per chat. No I/O, no asyncio — tests drive it with a
fake clock. One BoB turn = one batched flush (see ``bot.MessageBatcher``), so
the limiter is checked once per flush, before any gateway traffic.
"""
from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable

DEFAULT_PER_MINUTE = 10
DEFAULT_PER_DAY = 200
MINUTE_S = 60.0
DAY_S = 86400.0

RATE_LIMIT_MINUTE_REPLY = (
    "Easy there — that's a lot of turns. I take at most "
    f"{DEFAULT_PER_MINUTE} a minute; try again in a moment."
)
RATE_LIMIT_DAY_REPLY = (
    f"Daily turn limit reached ({DEFAULT_PER_DAY}). "
    "I'm done for today — the TUI still works."
)


class RateLimiter:
    """Sliding-window per-minute + UTC-day per-chat turn caps.

    ``check(chat_id)`` returns ``None`` when the turn is allowed (and records
    it), or the friendly refusal message when a cap is hit (not recorded —
    refused turns don't consume budget).
    """

    def __init__(
        self,
        per_minute: int = DEFAULT_PER_MINUTE,
        per_day: int = DEFAULT_PER_DAY,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if per_minute < 1 or per_day < 1:
            raise ValueError("rate limits must be >= 1")
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._minute: dict[int, deque[float]] = {}
        self._day: dict[int, tuple[int, int]] = {}  # chat_id -> (day bucket, count)

    def check(self, chat_id: int) -> str | None:
        now = self._clock()
        bucket = int(now // DAY_S)

        day_bucket, day_count = self._day.get(chat_id, (bucket, 0))
        if day_bucket != bucket:  # new UTC day — reset
            day_bucket, day_count = bucket, 0
        if day_count >= self.per_day:
            self._day[chat_id] = (day_bucket, day_count)
            return RATE_LIMIT_DAY_REPLY

        window = self._minute.setdefault(chat_id, deque())
        while window and now - window[0] >= MINUTE_S:
            window.popleft()
        if len(window) >= self.per_minute:
            return RATE_LIMIT_MINUTE_REPLY

        window.append(now)
        self._day[chat_id] = (day_bucket, day_count + 1)
        return None
