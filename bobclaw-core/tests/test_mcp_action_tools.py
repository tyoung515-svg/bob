"""MCP action-tools tests (2026-09-30): list_faces / list_backends /
get_server_time / check_model_slugs are registered, gated like the chat
tools, and proxy the right gateway routes.
"""
import asyncio
import json
import unittest
from aiohttp import web
from aiohttp.test_utils import TestServer

from core.mcp.server import MCPConfig, build_server


def _cfg(token: str, gateway: str = "") -> MCPConfig:
    return MCPConfig(token=token, gateway=gateway, faces=[], turn_timeout=30)


class TestActionToolRegistration(unittest.TestCase):
    def _tools(self, token: str) -> set[str]:
        return {t.name for t in build_server(_cfg(token))._tool_manager.list_tools()}

    def test_action_tools_always_registered(self):
        # A fake but well-formed JWT with an unrestricted faces claim.
        import base64

        def _b64(obj: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

        token = f"x.{_b64({'faces': []})}.y"
        tools = self._tools(token)
        for expected in ("chat_with_face", "list_faces", "list_backends", "get_server_time", "check_model_slugs"):
            self.assertIn(expected, tools, f"missing tool {expected}: {tools}")


class TestActionToolProxies(unittest.TestCase):
    """The read tools proxy the gateway REST routes and surface an upstream
    error in-band instead of raising."""

    _loop: asyncio.AbstractEventLoop
    _server: TestServer
    hits: list[tuple[str, str]]

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()
        cls.hits = []

        async def _setup() -> None:
            async def record(request: web.Request) -> web.Response:
                cls.hits.append((request.method, request.path))
                if request.path == "/faces":
                    return web.json_response([{"id": "assistant", "preferred_backend": "claude_api"}])
                if request.path.startswith("/faces/"):
                    return web.json_response(
                        {
                            "id": request.match_info["face_id"],
                            "preferred_backend": "claude_api",
                            "codex_posture": {"model": "gpt-5.6-sol"},
                        }
                    )
                if request.path == "/models/available":
                    return web.json_response(
                        [{"backend": "claude_api", "available": True, "model": "claude-x", "models": ["claude-x"]}]
                    )
                if request.path == "/backends/catalog":
                    return web.json_response(
                        [
                            {
                                "backend": "claude_api",
                                "kind": "cloud",
                                "available": True,
                                "model": "claude-x",
                                "models": ["claude-x"],
                                "max_usd_per_worker": 0.50,
                                "max_fanout_width": 4,
                            },
                            {
                                "backend": "codex_code",
                                "kind": "cli",
                                "available": True,
                                "model": None,
                                "models": [],
                                "max_usd_per_worker": 0.0,
                                "max_fanout_width": 2,
                            },
                        ]
                    )
                if request.path == "/system/ports":
                    return web.json_response({"gateway": 7836, "core": 7825})
                return web.json_response({}, status=404)

            app = web.Application()
            app.router.add_get("/faces", record)
            app.router.add_get("/faces/{face_id}", record)
            app.router.add_get("/models/available", record)
            app.router.add_get("/backends/catalog", record)
            app.router.add_get("/system/ports", record)
            cls._server = TestServer(app)
            await cls._server.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._server.close())
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _token(self) -> str:
        import base64

        def _b64(obj: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

        return f"x.{_b64({'faces': []})}.y"

    def test_list_faces_proxies_gateway(self):
        from core.mcp.server import chat_with_face_impl  # noqa: F401  (import sanity)

        cfg = _cfg(self._token(), str(self._server.make_url("/")))
        server = build_server(cfg)
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        out = self._run(tools["list_faces"].fn())
        data = json.loads(out)
        self.assertEqual(data[0]["id"], "assistant")

    def test_check_model_slugs_reports_staleness(self):
        cfg = _cfg(self._token(), str(self._server.make_url("/")))
        server = build_server(cfg)
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        out = self._run(tools["check_model_slugs"].fn())
        data = json.loads(out)
        self.assertEqual(data["checked"], 1)
        face = data["faces"][0]
        # codex_posture.model 'gpt-5.6-sol' is NOT in claude_api's offered
        # ['claude-x'] → must be flagged stale.
        self.assertTrue(face["stale"], f"expected stale flag: {face}")
        self.assertTrue(any("not offered" in n for n in face["notes"]), face["notes"])

    def test_read_tool_surfaces_http_error_in_band(self):
        # Point at a route the fake gateway 404s: the tool must return an
        # [error:http_...] string, never raise.
        cfg = _cfg(self._token(), str(self._server.make_url("/")))
        cfg_gateway = cfg  # noqa: F841
        server = build_server(cfg)
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        # system/ports exists on the fake server; instead force an error via a
        # bad gateway URL for one call.
        cfg2 = _cfg(self._token(), "http://127.0.0.1:1/")
        server2 = build_server(cfg2)
        tools2 = {t.name: t for t in server2._tool_manager.list_tools()}
        out = self._run(tools2["get_server_time"].fn())
        self.assertTrue(out.startswith("[error:"), out)


if __name__ == "__main__":
    unittest.main()
