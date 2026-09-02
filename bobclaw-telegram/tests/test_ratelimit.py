"""bobclaw-telegram — rate limiter tests (pure, fake clock; no I/O)."""
from __future__ import annotations

from bobclaw_telegram.ratelimit import (
    DAY_S,
    RATE_LIMIT_DAY_REPLY,
    RATE_LIMIT_MINUTE_REPLY,
    RateLimiter,
)


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_allows_up_to_per_minute_then_refuses():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=3, per_day=100, clock=clock)

    assert limiter.check(1) is None
    assert limiter.check(1) is None
    assert limiter.check(1) is None
    assert limiter.check(1) == RATE_LIMIT_MINUTE_REPLY


def test_minute_window_slides():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=2, per_day=100, clock=clock)

    assert limiter.check(1) is None
    clock.advance(30)
    assert limiter.check(1) is None
    assert limiter.check(1) == RATE_LIMIT_MINUTE_REPLY
    clock.advance(31)  # first turn fell out of the 60s window
    assert limiter.check(1) is None


def test_daily_cap_refuses_even_with_slow_pacing():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=10, per_day=5, clock=clock)

    for _ in range(5):
        assert limiter.check(1) is None
        clock.advance(61)  # never trip the per-minute window
    assert limiter.check(1) == RATE_LIMIT_DAY_REPLY


def test_daily_cap_resets_next_utc_day():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=10, per_day=2, clock=clock)

    assert limiter.check(1) is None
    assert limiter.check(1) is None
    assert limiter.check(1) == RATE_LIMIT_DAY_REPLY
    clock.advance(DAY_S)
    assert limiter.check(1) is None


def test_caps_are_per_chat():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=1, per_day=100, clock=clock)

    assert limiter.check(1) is None
    assert limiter.check(1) == RATE_LIMIT_MINUTE_REPLY
    assert limiter.check(2) is None  # a different chat is unaffected


def test_refused_turns_do_not_consume_budget():
    clock = FakeClock()
    limiter = RateLimiter(per_minute=2, per_day=100, clock=clock)

    assert limiter.check(1) is None
    assert limiter.check(1) is None
    for _ in range(50):  # refused turns pile up but consume nothing
        assert limiter.check(1) == RATE_LIMIT_MINUTE_REPLY
    clock.advance(61)
    assert limiter.check(1) is None


def test_daily_cap_checked_before_minute_window():
    # With the day cap hit, the day refusal wins even if the minute window
    # is also full.
    clock = FakeClock()
    limiter = RateLimiter(per_minute=2, per_day=2, clock=clock)

    assert limiter.check(1) is None
    assert limiter.check(1) is None
    assert limiter.check(1) == RATE_LIMIT_DAY_REPLY
