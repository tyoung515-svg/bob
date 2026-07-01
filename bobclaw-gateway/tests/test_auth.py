"""
Tests for BoBClaw Gateway authentication utilities and middleware.

Covers:
 - Password hashing / verification (bcrypt)
 - JWT creation and decoding
 - Expired token rejection
 - auth_middleware: allows /auth/* and /health without a token
 - auth_middleware: rejects missing / invalid tokens on protected routes
 - auth_middleware: accepts valid Bearer tokens
"""
import asyncio
import unittest
from datetime import datetime, timedelta, timezone

import jwt
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import (
    auth_middleware,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
    verify_password_plain,
)
from config import config


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


class TestPasswordHashing(unittest.TestCase):
    def test_hash_returns_bcrypt_string(self):
        hashed = hash_password("mypassword")
        self.assertIsInstance(hashed, str)
        self.assertTrue(hashed.startswith("$2b$"))

    def test_verify_correct_password(self):
        hashed = hash_password("mypassword")
        self.assertTrue(verify_password("mypassword", hashed))

    def test_verify_wrong_password(self):
        hashed = hash_password("mypassword")
        self.assertFalse(verify_password("wrongpassword", hashed))

    def test_verify_password_plain_correct(self):
        self.assertTrue(verify_password_plain(config.BOBCLAW_PASSWORD))

    def test_verify_password_plain_wrong(self):
        self.assertFalse(verify_password_plain("definitely_not_the_right_password"))


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------


class TestJWT(unittest.TestCase):
    def test_create_access_token_returns_string(self):
        token = create_access_token()
        self.assertIsInstance(token, str)

    def test_decode_valid_token(self):
        token = create_access_token("testuser")
        payload = decode_access_token(token)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sub"], "testuser")

    def test_decode_invalid_token_returns_none(self):
        result = decode_access_token("not.a.valid.token")
        self.assertIsNone(result)

    def test_decode_wrong_secret_returns_none(self):
        bad_token = jwt.encode(
            {
                "sub": "admin",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            "wrong-secret",
            algorithm="HS256",
        )
        result = decode_access_token(bad_token)
        self.assertIsNone(result)

    def test_expired_token_rejected(self):
        expired_token = jwt.encode(
            {
                "sub": "admin",
                "exp": datetime.now(timezone.utc) - timedelta(seconds=1),
            },
            config.JWT_SECRET,
            algorithm="HS256",
        )
        result = decode_access_token(expired_token)
        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Middleware (uses a minimal test app without DB hooks)
# Uses asyncio.run + TestClient/TestServer to avoid IsolatedAsyncioTestCase
# event-loop issues on Python 3.14.
# ---------------------------------------------------------------------------


def _make_middleware_app() -> web.Application:
    """Minimal test app wired with auth_middleware."""
    app = web.Application(middlewares=[auth_middleware])

    async def protected(request: web.Request) -> web.Response:
        return web.json_response({"user": request["user"]["sub"]})

    async def auth_handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def health_handler(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def ws_handler(request: web.Request) -> web.Response:
        # Stand-in for /ws/chat — middleware should bypass auth here
        return web.json_response({"ws": "upgraded"})

    app.router.add_get("/protected", protected)
    app.router.add_get("/auth/test", auth_handler)
    app.router.add_post("/auth/login", auth_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/ws/chat", ws_handler)
    return app


class TestAuthMiddleware(unittest.TestCase):
    """One shared event loop + TestClient for all tests in this class."""

    _loop: asyncio.AbstractEventLoop
    _client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()
        async def _setup():
            cls._client = TestClient(TestServer(_make_middleware_app()))
            await cls._client.start_server()
        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def test_auth_route_bypasses_middleware(self):
        resp = self._run(self._client.get("/auth/test"))
        self.assertEqual(resp.status, 200)

    def test_auth_post_bypasses_middleware(self):
        resp = self._run(self._client.post("/auth/login", json={}))
        self.assertEqual(resp.status, 200)

    def test_health_bypasses_middleware(self):
        resp = self._run(self._client.get("/health"))
        self.assertEqual(resp.status, 200)

    def test_protected_missing_auth_header(self):
        resp = self._run(self._client.get("/protected"))
        self.assertEqual(resp.status, 401)

    def test_protected_bad_scheme(self):
        resp = self._run(
            self._client.get("/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        )
        self.assertEqual(resp.status, 401)

    def test_protected_invalid_token(self):
        resp = self._run(
            self._client.get("/protected", headers={"Authorization": "Bearer invalid.token.here"})
        )
        self.assertEqual(resp.status, 401)

    def test_protected_expired_token(self):
        expired = jwt.encode(
            {"sub": "admin", "exp": datetime.now(timezone.utc) - timedelta(seconds=1)},
            config.JWT_SECRET,
            algorithm="HS256",
        )
        resp = self._run(
            self._client.get("/protected", headers={"Authorization": f"Bearer {expired}"})
        )
        self.assertEqual(resp.status, 401)

    def test_protected_valid_token(self):
        token = create_access_token("testuser")
        resp = self._run(
            self._client.get("/protected", headers={"Authorization": f"Bearer {token}"})
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["user"], "testuser")

    def test_ws_chat_bypasses_middleware(self):
        """/ws/chat must reach the handler without a token (auth happens inside)."""
        resp = self._run(self._client.get("/ws/chat"))
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["ws"], "upgraded")


# ---------------------------------------------------------------------------
# TOTP replay protection (uses full gateway app with SQLite)
# ---------------------------------------------------------------------------


class TestTOTPReplayProtection(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup():
            from gateway import build_app
            app = build_app()
            cls._client = TestClient(TestServer(app))
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    @staticmethod
    def _valid_code() -> str:
        import pyotp
        from config import config
        return pyotp.TOTP(config.TOTP_SECRET).now()

    def _login(self, code: str) -> int:
        resp = self._run(
            self._client.post(
                "/auth/login",
                json={"password": config.BOBCLAW_PASSWORD, "totp_code": code},
            )
        )
        return resp.status

    def test_first_login_succeeds(self):
        code = self._valid_code()
        self.assertEqual(self._login(code), 200)

    def test_replay_same_code_rejected(self):
        code = self._valid_code()
        self.assertEqual(self._login(code), 200)
        self.assertEqual(self._login(code), 401)

    def test_next_timestep_code_accepted(self):
        import time as time_mod
        from unittest import mock

        code1 = self._valid_code()
        self.assertEqual(self._login(code1), 200)

        with mock.patch.object(time_mod, "time", return_value=time_mod.time() + 31):
            code2 = self._valid_code()
            self.assertEqual(self._login(code2), 200)

    def test_persistence_across_connections(self):
        code1 = self._valid_code()
        self.assertEqual(self._login(code1), 200)

        # Replay should still be rejected (same process, same SQLite file)
        self.assertEqual(self._login(code1), 401)

    def test_nonexistent_user_has_no_stored_timestep(self):
        from db import check_totp_replay
        is_replay = self._run(check_totp_replay("nonexistent_user", 999999))
        self.assertFalse(is_replay)
