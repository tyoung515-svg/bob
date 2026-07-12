"""BoBClaw Gateway — memory-graph proxy tests (U4a).

Verifies GET /memory/graph requires a JWT, forwards query params to core, and
passes the assembled graph shape + status + content-type straight through (a 400
invalid-request is preserved, not coerced to 502; only a real core-connection
failure surfaces as 502).
"""
import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import create_access_token
from config import config
from gateway import build_app


class TestMemoryGraphProxy(unittest.TestCase):
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

            async def core_graph(request: web.Request) -> web.Response:
                cls.core_calls.append(dict(request.query))
                if request.query.get("nodes") == "bad":
                    return web.json_response(
                        {"type": "error", "message": "nodes must be an int",
                         "code": "invalid_request"},
                        status=400,
                    )
                return web.json_response({
                    "nodes": [
                        {"id": "fact:f1", "type": "fact", "label": "a fact", "payload": {}},
                        {"id": "conversation:e1", "type": "conversation", "label": "hi", "payload": {}},
                    ],
                    "edges": [
                        {"source": "fact:f1", "target": "conversation:e1", "type": "provenance"},
                    ],
                    "meta": {"node_count": 2, "edge_count": 1, "node_cap": 500},
                })

            core_app = web.Application()
            core_app.router.add_get("/api/memory/graph", core_graph)
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

    def test_requires_jwt(self):
        resp = self._run(self._client.get("/memory/graph"))
        self.assertEqual(resp.status, 401)

    # ── forwarding + shape ─────────────────────────────────────────────────────

    def test_returns_assembled_graph_shape(self):
        resp = self._run(self._client.get("/memory/graph", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        body = self._run(resp.json())
        self.assertIn("nodes", body)
        self.assertIn("edges", body)
        self.assertIn("meta", body)
        self.assertEqual(body["nodes"][0]["type"], "fact")
        self.assertEqual(body["edges"][0]["type"], "provenance")
        self.assertEqual(body["meta"]["node_count"], 2)

    def test_forwards_query_params(self):
        self._run(self._client.get(
            "/memory/graph?nodes=50&k=3&floor=0.5&types=fact,conversation",
            headers=self._auth()))
        self.assertEqual(
            self.core_calls[0],
            {"nodes": "50", "k": "3", "floor": "0.5", "types": "fact,conversation"},
        )

    def test_invalid_param_passes_through_400(self):
        resp = self._run(self._client.get("/memory/graph?nodes=bad", headers=self._auth()))
        self.assertEqual(resp.status, 400)  # not coerced to 502
        self.assertEqual(self._run(resp.json())["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
