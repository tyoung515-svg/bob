"""BoBClaw Gateway — JOAT v0 routing-view proxy tests.

Verifies GET /routing-view requires a JWT, forwards ?team/?format to core, and
passes the upstream payload + status + content-type straight through (a 400
unknown-team is preserved, not coerced to 502).
"""
import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import create_access_token
from config import config
from gateway import build_app


class TestRoutingViewProxy(unittest.TestCase):
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

            async def core_routing_view(request: web.Request) -> web.Response:
                cls.core_calls.append(dict(request.query))
                if request.query.get("team") == "nope":
                    return web.json_response(
                        {"type": "error", "message": "Unknown team", "code": "unknown_team"},
                        status=400,
                    )
                if request.query.get("format") == "text":
                    return web.Response(text="active_team: cloud-heavy\nFACE ...\n",
                                        content_type="text/plain")
                return web.json_response({
                    "active_team": request.query.get("team"),
                    "teams": ["cloud-heavy", "local-first"],
                    "faces": [{"id": "worker-deepseek", "role": "worker",
                               "resolved_backend": "glm_5_2",
                               "escalation_chain": ["deepseek_v4_flash"],
                               "tool_capable": True}],
                })

            core_app = web.Application()
            core_app.router.add_get("/api/routing-view", core_routing_view)
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

    def test_requires_jwt(self):
        resp = self._run(self._client.get("/routing-view"))
        self.assertEqual(resp.status, 401)

    def test_forwards_and_returns_core_payload(self):
        resp = self._run(self._client.get("/routing-view", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        body = self._run(resp.json())
        self.assertEqual(body["teams"], ["cloud-heavy", "local-first"])
        self.assertEqual(body["faces"][0]["resolved_backend"], "glm_5_2")

    def test_forwards_team_and_format_query(self):
        self._run(self._client.get(
            "/routing-view?team=cloud-heavy", headers=self._auth()))
        self.assertEqual(self.core_calls[0], {"team": "cloud-heavy"})

    def test_text_format_passes_through_content_type(self):
        resp = self._run(self._client.get(
            "/routing-view?format=text", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "text/plain")
        self.assertIn("active_team: cloud-heavy", self._run(resp.text()))

    def test_unknown_team_passes_through_400(self):
        resp = self._run(self._client.get(
            "/routing-view?team=nope", headers=self._auth()))
        self.assertEqual(resp.status, 400)  # not coerced to 502
        self.assertEqual(self._run(resp.json())["code"], "unknown_team")


if __name__ == "__main__":
    unittest.main()
