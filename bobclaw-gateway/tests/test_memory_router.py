"""
BoBClaw Gateway — memory facts proxy tests.

Verifies GET/DELETE /memory/facts require a JWT and forward to core, preserving
the upstream status (e.g. a 404 "unknown fact" passes through, not a 502).
"""
import asyncio
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import create_access_token
from config import config
from gateway import build_app


class TestMemoryProxy(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient
    _core_server: TestServer
    _original_core_url: str
    core_calls: list

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            cls.core_calls = []

            async def core_list(request: web.Request) -> web.Response:
                cls.core_calls.append(("GET", dict(request.query)))
                return web.json_response([
                    {"fact_id": "f1", "text": "a fact", "subject": "s",
                     "predicate": "p", "ts": "2026-06-15T00:00:00+00:00",
                     "source_event_id": "evt-f1", "confidence": {"rank": "normal"}},
                ])

            async def core_forget(request: web.Request) -> web.Response:
                fact_id = request.match_info["fact_id"]
                cls.core_calls.append(("DELETE", fact_id))
                if fact_id == "missing":
                    return web.json_response(
                        {"type": "error", "message": "Unknown fact_id: missing",
                         "code": "fact_not_found"},
                        status=404,
                    )
                return web.json_response({"status": "forgotten", "fact_id": fact_id})

            core_app = web.Application()
            core_app.router.add_get("/api/memory/facts", core_list)
            core_app.router.add_delete("/api/memory/facts/{fact_id}", core_forget)
            cls._core_server = TestServer(core_app)
            await cls._core_server.start_server()

            cls._original_core_url = config.CORE_URL
            config.CORE_URL = str(cls._core_server.make_url("/")).rstrip("/")

            cls._client = TestClient(TestServer(build_app()))
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        async def _teardown() -> None:
            config.CORE_URL = cls._original_core_url
            await cls._client.close()
            await cls._core_server.close()

        cls._loop.run_until_complete(_teardown())
        cls._loop.close()

    def setUp(self) -> None:
        self.core_calls.clear()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth(self):
        return {"Authorization": f"Bearer {create_access_token('bobclaw')}"}

    # ── auth ──────────────────────────────────────────────────────────────────

    def test_list_requires_jwt(self):
        resp = self._run(self._client.get("/memory/facts"))
        self.assertEqual(resp.status, 401)

    def test_delete_requires_jwt(self):
        resp = self._run(self._client.delete("/memory/facts/f1"))
        self.assertEqual(resp.status, 401)

    # ── forwarding ────────────────────────────────────────────────────────────

    def test_list_forwards_and_returns_core_payload(self):
        resp = self._run(self._client.get("/memory/facts", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        body = self._run(resp.json())
        self.assertEqual(body[0]["fact_id"], "f1")
        self.assertEqual(self.core_calls[0][0], "GET")

    def test_list_forwards_pagination_params(self):
        self._run(self._client.get(
            "/memory/facts?limit=10&offset=5", headers=self._auth()))
        self.assertEqual(self.core_calls[0], ("GET", {"limit": "10", "offset": "5"}))

    def test_delete_forwards_and_returns_forgotten(self):
        resp = self._run(self._client.delete(
            "/memory/facts/known", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._run(resp.json()),
                         {"status": "forgotten", "fact_id": "known"})
        self.assertEqual(self.core_calls[0], ("DELETE", "known"))

    def test_delete_passes_through_404(self):
        resp = self._run(self._client.delete(
            "/memory/facts/missing", headers=self._auth()))
        self.assertEqual(resp.status, 404)  # not coerced to 502
        self.assertEqual(self._run(resp.json())["code"], "fact_not_found")


if __name__ == "__main__":
    unittest.main()
