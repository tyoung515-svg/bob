"""
BoBClaw Gateway — Audit-log middleware tests
"""
from __future__ import annotations

import json
import logging

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from audit_log import make_audit_log_middleware


# ─── Test handlers ────────────────────────────────────────────────────────────

async def _ok_handler(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def _boom_handler(request: web.Request) -> web.Response:
    raise RuntimeError("boom")


async def _http_404_handler(request: web.Request) -> web.Response:
    raise web.HTTPNotFound(text="missing")


async def _make_client(enabled: bool = True) -> TestClient:
    app = web.Application(middlewares=[make_audit_log_middleware(enabled=enabled)])
    app.router.add_get("/ok", _ok_handler)
    app.router.add_get("/boom", _boom_handler)
    app.router.add_get("/missing", _http_404_handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _last_audit_record(caplog) -> dict:
    """Pull the last audit log record's JSON payload."""
    audit_records = [
        r for r in caplog.records if r.name == "bobclaw.gateway.audit"
    ]
    assert audit_records, "no audit records emitted"
    return json.loads(audit_records[-1].getMessage())


# ─── Tests ────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_audit_logs_successful_request(caplog):
    client = await _make_client()
    try:
        with caplog.at_level(logging.INFO, logger="bobclaw.gateway.audit"):
            resp = await client.get("/ok")
        assert resp.status == 200
        assert "X-Request-ID" in resp.headers
        rec = _last_audit_record(caplog)
        assert rec["method"] == "GET"
        assert rec["path"] == "/ok"
        assert rec["status"] == 200
        assert rec["request_id"] == resp.headers["X-Request-ID"]
        assert rec["user"] is None
        assert isinstance(rec["duration_ms"], int)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_audit_logs_unhandled_exception_as_500(caplog):
    client = await _make_client()
    try:
        with caplog.at_level(logging.INFO, logger="bobclaw.gateway.audit"):
            resp = await client.get("/boom")
        assert resp.status == 500
        rec = _last_audit_record(caplog)
        assert rec["status"] == 500
        assert rec["error_class"] == "RuntimeError"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_audit_logs_http_exception_with_correct_status(caplog):
    client = await _make_client()
    try:
        with caplog.at_level(logging.INFO, logger="bobclaw.gateway.audit"):
            resp = await client.get("/missing")
        assert resp.status == 404
        rec = _last_audit_record(caplog)
        assert rec["status"] == 404
        assert rec["error_class"] == "HTTPNotFound"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_audit_disabled_emits_nothing(caplog):
    client = await _make_client(enabled=False)
    try:
        with caplog.at_level(logging.INFO, logger="bobclaw.gateway.audit"):
            resp = await client.get("/ok")
        assert resp.status == 200
        assert "X-Request-ID" not in resp.headers
        records = [r for r in caplog.records if r.name == "bobclaw.gateway.audit"]
        assert records == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_audit_request_id_unique_per_request(caplog):
    client = await _make_client()
    try:
        with caplog.at_level(logging.INFO, logger="bobclaw.gateway.audit"):
            r1 = await client.get("/ok")
            r2 = await client.get("/ok")
        assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]
    finally:
        await client.close()
