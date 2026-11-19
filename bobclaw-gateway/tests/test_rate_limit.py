"""
BoBClaw Gateway — Rate limit middleware tests
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace

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
        seconds = int(r2.headers["Retry-After"])
        assert seconds >= 1
        assert body == {"error": "rate limit exceeded", "code": "rate_limited",
                        "retry_after_seconds": seconds}
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


@pytest.mark.asyncio
async def test_refusal_rounds_up_and_never_calls_handler(monkeypatch):
    calls = []
    clock = [0.0]
    consume = TokenBucket.consume
    monkeypatch.setattr(TokenBucket, "consume", lambda self, key: consume(self, key, now=clock[0]))
    middleware = make_rate_limit_middleware(30, 1)
    request = {"user": {"sub": "owner"}}
    class Request(dict):
        path = "/echo"
        match_info = SimpleNamespace(route=SimpleNamespace(
            method="GET", resource=SimpleNamespace(canonical="/echo")))
    request = Request(request)
    async def handler(request):
        calls.append(request)
        return web.json_response({"ok": True})
    assert (await middleware(request, handler)).status == 200
    clock[0] = 0.25
    refused = await middleware(request, handler)
    assert refused.headers["Retry-After"] == "2"
    assert json.loads(refused.text)["retry_after_seconds"] == 2
    clock[0] = 2.0
    assert (await middleware(request, handler)).status == 200
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_debug_counter_groups_route_templates_and_counts_refusals(caplog):
    caplog.set_level(logging.DEBUG, logger="rate_limit")
    app = web.Application(middlewares=[make_rate_limit_middleware(1, 1)])
    app.router.add_get("/conversations/{conversation_id}", _ok_handler)
    app.router.add_get("/health", _ok_handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        assert (await client.get("/conversations/private-a?secret=query-canary")).status == 200
        assert (await client.get("/conversations/private-b")).status == 429
        assert (await client.get("/health")).status == 200
        messages = [r.getMessage() for r in caplog.records if r.name == "rate_limit"]
        assert messages == [
            "rate_limit request method=GET route=/conversations/{conversation_id} count=1",
            "rate_limit request method=GET route=/conversations/{conversation_id} count=2",
        ]
        assert not any("private-a" in m or "private-b" in m or "query-canary" in m for m in messages)
    finally:
        await client.close()
