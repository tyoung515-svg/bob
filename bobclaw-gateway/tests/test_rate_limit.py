"""
BoBClaw Gateway — Rate limit middleware tests
"""
from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from rate_limit import TokenBucket, make_rate_limit_middleware


# ─── TokenBucket math ─────────────────────────────────────────────────────────

def test_token_bucket_allows_up_to_burst():
    b = TokenBucket(rate_per_minute=60, burst=3)
    # Same timestamp — no refill, only the initial burst available
    assert b.consume("k", now=0.0)[0] is True
    assert b.consume("k", now=0.0)[0] is True
    assert b.consume("k", now=0.0)[0] is True
    allowed, retry_after = b.consume("k", now=0.0)
    assert allowed is False
    assert retry_after > 0


def test_token_bucket_refills_over_time():
    b = TokenBucket(rate_per_minute=60, burst=1)
    assert b.consume("k", now=0.0)[0] is True
    # 1 token/second at rate=60/min — 1.0 second later, refilled
    assert b.consume("k", now=1.0)[0] is True


def test_token_bucket_independent_keys():
    b = TokenBucket(rate_per_minute=60, burst=1)
    assert b.consume("a", now=0.0)[0] is True
    assert b.consume("b", now=0.0)[0] is True  # different key, fresh burst


# ─── Middleware end-to-end ────────────────────────────────────────────────────

async def _ok_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _make_client(rate_per_minute: int = 60, burst: int = 2) -> TestClient:
    app = web.Application(
        middlewares=[make_rate_limit_middleware(rate_per_minute, burst)]
    )
    app.router.add_get("/echo", _ok_handler)
    app.router.add_get("/health", _ok_handler)
    app.router.add_get("/ws/chat", _ok_handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


@pytest.mark.asyncio
async def test_middleware_allows_under_burst():
    client = await _make_client(rate_per_minute=60, burst=2)
    try:
        r1 = await client.get("/echo")
        r2 = await client.get("/echo")
        assert r1.status == 200
        assert r2.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_middleware_blocks_after_burst():
    client = await _make_client(rate_per_minute=60, burst=1)
    try:
        r1 = await client.get("/echo")
        r2 = await client.get("/echo")
        assert r1.status == 200
        assert r2.status == 429
        assert "Retry-After" in r2.headers
        body = await r2.json()
        assert body["error"] == "rate limit exceeded"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_health_bypasses_rate_limit():
    client = await _make_client(rate_per_minute=60, burst=1)
    try:
        # Burn the bucket
        await client.get("/echo")
        await client.get("/echo")  # this is 429
        # Health still works
        h = await client.get("/health")
        assert h.status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_ws_chat_bypasses_rate_limit():
    client = await _make_client(rate_per_minute=60, burst=1)
    try:
        await client.get("/echo")
        await client.get("/echo")  # 429
        ws = await client.get("/ws/chat")
        assert ws.status == 200
    finally:
        await client.close()
