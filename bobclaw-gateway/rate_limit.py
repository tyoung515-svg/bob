"""
BoBClaw Gateway — Rate limiting middleware (in-memory token bucket)

Per-user (JWT sub) or per-IP token bucket. Per-process state — NOT shared
across processes. For multi-process deployments, the effective limit is
N_processes * RATE_LIMIT_PER_MINUTE per user. Acceptable as a P2 hygiene
backstop; replace with a Redis-backed bucket if abuse prevention becomes
load-bearing.
"""
from __future__ import annotations

import math
import time
from typing import Awaitable, Callable

from aiohttp import web

# Paths the rate limiter does not see. /health is skipped to avoid throttling
# k8s liveness probes; /ws/chat is a long-lived WS handshake (rate-limit per
# connection makes no sense — message-level limits would be a different layer);
# /ui static assets are public files (not API actions) and a single page load
# pulls several — rate-limiting them only risks throttling legitimate loads.
_BYPASS_PATHS: frozenset[str] = frozenset({"/health"})
_BYPASS_PREFIXES: tuple[str, ...] = ("/ws/chat", "/ui")


class TokenBucket:
    """In-memory token bucket keyed by an arbitrary identifier string."""

    def __init__(self, rate_per_minute: int, burst: int) -> None:
        if rate_per_minute <= 0 or burst <= 0:
            raise ValueError("rate_per_minute and burst must be positive")
        self._rate_per_sec: float = rate_per_minute / 60.0
        self._burst: float = float(burst)
        # key -> (tokens, last_refill_monotonic_seconds)
        self._buckets: dict[str, tuple[float, float]] = {}

    def consume(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """Try to consume one token from *key*'s bucket.

        Returns (allowed, retry_after_seconds). retry_after_seconds is 0
        when allowed, else the wall-clock seconds until the next token
        refill (rounded up by the caller for the Retry-After header).
        """
        now = now if now is not None else time.monotonic()
        tokens, last = self._buckets.get(key, (self._burst, now))
        elapsed = max(0.0, now - last)
        tokens = min(self._burst, tokens + elapsed * self._rate_per_sec)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0
        deficit = 1.0 - tokens
        retry_after = deficit / self._rate_per_sec
        self._buckets[key] = (tokens, now)
        return False, retry_after


def _request_key(request: web.Request) -> str:
    user = request.get("user")
    if user and "sub" in user:
        return f"user:{user['sub']}"
    remote = request.remote or "unknown"
    return f"ip:{remote}"


def _is_bypassed(path: str) -> bool:
    if path in _BYPASS_PATHS:
        return True
    for prefix in _BYPASS_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def make_rate_limit_middleware(
    rate_per_minute: int,
    burst: int,
) -> Callable[[web.Request, Callable], Awaitable[web.StreamResponse]]:
    """Factory: build an aiohttp middleware bound to a fresh TokenBucket."""
    bucket = TokenBucket(rate_per_minute=rate_per_minute, burst=burst)

    @web.middleware
    async def rate_limit_middleware(request: web.Request, handler):
        if _is_bypassed(request.path):
            return await handler(request)

        allowed, retry_after = bucket.consume(_request_key(request))
        if not allowed:
            return web.json_response(
                {"error": "rate limit exceeded"},
                status=429,
                headers={"Retry-After": str(max(1, math.ceil(retry_after)))},
            )
        return await handler(request)

    return rate_limit_middleware
