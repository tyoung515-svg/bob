"""
Integration tests for the BoBClaw Gateway.

Exercises the full app — middleware, startup hooks (SQLite init), all routers.
Uses a class-level shared event loop + TestClient/TestServer to avoid
IsolatedAsyncioTestCase event-loop issues on Python 3.14.

Covers:
 - GET /health → 200
 - POST /auth/login correct password → tokens
 - POST /auth/login wrong password → 401
 - Protected routes require a valid token
 - Token rotation via POST /auth/refresh
"""
import asyncio
import unittest

import pyotp
from aiohttp.test_utils import TestClient, TestServer

from auth import create_access_token
from config import config
from gateway import build_app


def _make_app():
    """Build a fresh gateway app for each test class."""
    return build_app()


def _totp_code() -> str:
    """Current TOTP code for the configured TOTP_SECRET, or "" if unset."""
    if not config.TOTP_SECRET:
        return ""
    return pyotp.TOTP(config.TOTP_SECRET).now()


class _GatewayTestBase(unittest.TestCase):
    """Base: one shared event loop + TestClient per concrete subclass."""

    _loop: asyncio.AbstractEventLoop
    _client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            cls._client = TestClient(TestServer(_make_app()))
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)


# ── /health ──────────────────────────────────────────────────────────────────

class TestHealthEndpoint(_GatewayTestBase):

    def test_health_returns_200(self):
        resp = self._run(self._client.get("/health"))
        self.assertEqual(resp.status, 200)

    def test_health_body(self):
        resp = self._run(self._client.get("/health"))
        data = self._run(resp.json())
        self.assertIn("status", data)
        self.assertEqual(data["status"], "ok")

    def test_health_does_not_leak_internal_urls(self):
        # Security (A1): the unauthenticated /health must NOT expose internal
        # service URLs (recon surface behind a reverse proxy). The service map
        # lives behind auth at /system/config.
        resp = self._run(self._client.get("/health"))
        data = self._run(resp.json())
        self.assertNotIn("services", data)
        self.assertEqual(set(data.keys()), {"status", "service"})

    def test_security_headers_present(self):
        # Security (A2): every response carries the security headers.
        resp = self._run(self._client.get("/health"))
        self.assertIn("default-src 'self'", resp.headers.get("Content-Security-Policy", ""))
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "no-referrer")


# ── POST /auth/login ──────────────────────────────────────────────────────────

class TestAuthLogin(_GatewayTestBase):

    def test_login_correct_password(self):
        resp = self._run(
            self._client.post(
                "/auth/login",
                json={
                    "password": config.BOBCLAW_PASSWORD,
                    "totp_code": _totp_code(),
                },
            )
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertIn("access_token", data)
        self.assertIn("refresh_token", data)

    def test_login_wrong_password_401(self):
        resp = self._run(
            self._client.post("/auth/login", json={"password": "wrongpassword"})
        )
        self.assertEqual(resp.status, 401)

    def test_login_empty_password_401(self):
        resp = self._run(
            self._client.post("/auth/login", json={"password": ""})
        )
        self.assertEqual(resp.status, 401)

    def test_login_invalid_json_400(self):
        resp = self._run(
            self._client.post(
                "/auth/login",
                data=b"not-json",
                headers={"Content-Type": "application/json"},
            )
        )
        self.assertEqual(resp.status, 400)


# ── GET /auth/status ──────────────────────────────────────────────────────────

class TestAuthStatus(_GatewayTestBase):

    def test_status_no_token(self):
        resp = self._run(self._client.get("/auth/status"))
        self.assertEqual(resp.status, 401)

    def test_status_with_valid_token(self):
        token = create_access_token("bobclaw")
        resp = self._run(
            self._client.get(
                "/auth/status",
                headers={"Authorization": f"Bearer {token}"},
            )
        )
        self.assertEqual(resp.status, 200)


# ── Protected routes ──────────────────────────────────────────────────────────

class TestProtectedRoutes(_GatewayTestBase):

    def _auth_headers(self):
        token = create_access_token("bobclaw")
        return {"Authorization": f"Bearer {token}"}

    def test_system_ports_no_auth_401(self):
        resp = self._run(self._client.get("/system/ports"))
        self.assertEqual(resp.status, 401)

    def test_system_ports_with_valid_token(self):
        resp = self._run(
            self._client.get("/system/ports", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)

    def test_system_config_no_auth_401(self):
        resp = self._run(self._client.get("/system/config"))
        self.assertEqual(resp.status, 401)

    def test_system_config_with_valid_token(self):
        resp = self._run(
            self._client.get("/system/config", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)


# ── Token refresh / rotation ──────────────────────────────────────────────────

class TestTokenRefresh(_GatewayTestBase):

    def _login(self):
        resp = self._run(
            self._client.post(
                "/auth/login",
                json={
                    "password": config.BOBCLAW_PASSWORD,
                    "totp_code": _totp_code(),
                },
            )
        )
        return self._run(resp.json())

    def test_refresh_returns_new_tokens(self):
        tokens = self._login()
        resp = self._run(
            self._client.post(
                "/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
            )
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertIn("access_token", data)
        self.assertIn("refresh_token", data)

    def test_refresh_rotates_token(self):
        tokens = self._login()
        resp = self._run(
            self._client.post(
                "/auth/refresh",
                json={"refresh_token": tokens["refresh_token"]},
            )
        )
        data = self._run(resp.json())
        self.assertNotEqual(data["refresh_token"], tokens["refresh_token"])

    def test_refresh_bad_token_401(self):
        resp = self._run(
            self._client.post(
                "/auth/refresh",
                json={"refresh_token": "not-a-real-token"},
            )
        )
        self.assertEqual(resp.status, 401)

    def test_refresh_old_token_rejected_after_rotation(self):
        tokens = self._login()
        old_refresh = tokens["refresh_token"]
        self._run(
            self._client.post(
                "/auth/refresh",
                json={"refresh_token": old_refresh},
            )
        )
        resp = self._run(
            self._client.post(
                "/auth/refresh",
                json={"refresh_token": old_refresh},
            )
        )
        self.assertEqual(resp.status, 401)

    def test_logout_invalidates_refresh_token(self):
        tokens = self._login()
        access_token = tokens["access_token"]
        refresh_token = tokens["refresh_token"]

        self._run(
            self._client.post(
                "/auth/logout",
                json={"refresh_token": refresh_token},
                headers={"Authorization": f"Bearer {access_token}"},
            )
        )
        resp = self._run(
            self._client.post(
                "/auth/refresh",
                json={"refresh_token": refresh_token},
            )
        )
        self.assertEqual(resp.status, 401)


# ── CORS ─────────────────────────────────────────────────────────────────────

class TestCORS(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _test_preflight(self, allowed_origins, request_origin, expect_origin, expect_status=200):
        from config import config as cfg
        original = cfg.ALLOWED_ORIGINS
        cfg.ALLOWED_ORIGINS = allowed_origins
        try:
            async def _inner():
                app = build_app()
                client = TestClient(TestServer(app))
                await client.start_server()
                try:
                    resp = await client.options(
                        "/health",
                        headers={
                            "Origin": request_origin,
                            "Access-Control-Request-Method": "GET",
                            "Access-Control-Request-Headers": "Authorization",
                        },
                    )
                    return resp
                finally:
                    await client.close()

            self._loop = asyncio.new_event_loop()
            try:
                resp = self._run(_inner())
                self.assertEqual(resp.status, expect_status)
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), expect_origin)
            finally:
                self._loop.close()
        finally:
            cfg.ALLOWED_ORIGINS = original

    def test_preflight_allowed_origin(self):
        self._test_preflight(
            ["http://localhost:3000"],
            "http://localhost:3000",
            "http://localhost:3000",
        )

    def test_preflight_disallowed_origin(self):
        self._test_preflight(
            ["http://localhost:3000"],
            "http://evil.com",
            None,
            expect_status=403,
        )


# ── OpenCode pool close ───────────────────────────────────────────────────────

class TestOpenCodePoolClose(unittest.TestCase):

    def test_on_cleanup_awaits_opencode_pool_close(self):
        from unittest.mock import AsyncMock, patch

        async def _inner():
            app = build_app()
            with patch("gateway._opencode_pool.close", new_callable=AsyncMock) as mock_close:
                from gateway import _on_cleanup
                await _on_cleanup(app)
                mock_close.assert_awaited_once()

        _loop = asyncio.new_event_loop()
        try:
            _loop.run_until_complete(_inner())
        finally:
            _loop.close()
