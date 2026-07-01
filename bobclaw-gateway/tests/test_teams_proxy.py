"""BoBClaw Gateway — JOAT team-store proxy tests.

Verifies /teams requires a JWT, forwards GET/POST/DELETE to core, and passes the
upstream payload + status straight through (a 400 invalid-team / 404 not-found is
preserved, not coerced to 502). Mirrors test_routing_view_proxy.py.
"""
import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import create_access_token
from config import config
from gateway import build_app


class TestTeamsProxy(unittest.TestCase):
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

            async def core_teams(request: web.Request) -> web.Response:
                if request.method == "POST":
                    body = await request.json()
                    cls.core_calls.append(("POST", body))
                    if body.get("name") == "bad":
                        return web.json_response(
                            {"type": "error", "message": "invalid", "code": "invalid_team"},
                            status=400,
                        )
                    return web.json_response(
                        {"name": body["name"], "builtin": False, "roles": body.get("roles", {})},
                        status=201,
                    )
                cls.core_calls.append(("GET", dict(request.query)))
                return web.json_response({"items": [
                    {"name": "demo-fleet", "builtin": True,
                     "roles": {"worker": {"backend": "deepseek_v4_flash", "escalation_chain": []}}},
                ]})

            async def core_delete(request: web.Request) -> web.Response:
                name = request.match_info["name"]
                cls.core_calls.append(("DELETE", name))
                if name == "missing":
                    return web.json_response({"type": "error", "code": "not_found"}, status=404)
                return web.json_response({"status": "deleted", "name": name})

            async def core_backends(request: web.Request) -> web.Response:
                cls.core_calls.append(("GET-backends", dict(request.query)))
                return web.json_response({
                    "items": [{"backend": "local", "max_usd_per_worker": 0.0, "max_fanout_width": 1}],
                    "roles": ["apex", "worker", "critic"],
                })

            async def core_propose(request: web.Request) -> web.Response:
                body = await request.json()
                cls.core_calls.append(("PROPOSE", body))
                return web.json_response({
                    "goal": body.get("goal", ""), "name": "auto-fleet",
                    "roles": {"worker": {"backend": "local", "escalation_chain": []}},
                    "raw": "{}",
                })

            core_app = web.Application()
            core_app.router.add_get("/api/teams", core_teams)
            core_app.router.add_post("/api/teams", core_teams)
            core_app.router.add_delete("/api/teams/{name}", core_delete)
            core_app.router.add_get("/api/backends", core_backends)
            core_app.router.add_post("/api/teams/propose", core_propose)

            async def core_refine(request: web.Request) -> web.Response:
                body = await request.json()
                cls.core_calls.append(("REFINE", body))
                return web.json_response({
                    "reply": "ok",
                    "draft": {"name": "d", "roles": {
                        "worker": [{"name": "", "backend": "local", "escalation_chain": []}]}},
                    "raw": "{}",
                })
            core_app.router.add_post("/api/teams/refine", core_refine)

            async def core_profiles(request: web.Request) -> web.Response:
                if request.method == "POST":
                    body = await request.json()
                    cls.core_calls.append(("POST-profiles", body))
                    return web.json_response(
                        {"name": body.get("name"), "builtin": False,
                         "roles": body.get("roles", {}), "shape": body.get("shape")},
                        status=201,
                    )
                cls.core_calls.append(("GET-profiles", dict(request.query)))
                return web.json_response({"items": [{"name": "demo-fleet", "builtin": True, "roles": {}}]})
            core_app.router.add_get("/api/profiles", core_profiles)
            core_app.router.add_post("/api/profiles", core_profiles)
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
        self.assertEqual(self._run(self._client.get("/teams")).status, 401)

    def test_list_forwards_and_returns_payload(self):
        resp = self._run(self._client.get("/teams", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        body = self._run(resp.json())
        self.assertEqual(body["items"][0]["name"], "demo-fleet")

    def test_create_forwards_body_and_201(self):
        resp = self._run(self._client.post(
            "/teams",
            json={"name": "my-fleet", "roles": {"worker": {"backend": "local"}}},
            headers=self._auth(),
        ))
        self.assertEqual(resp.status, 201)
        self.assertEqual(self._run(resp.json())["name"], "my-fleet")
        self.assertEqual(self.core_calls[-1][0], "POST")
        self.assertEqual(self.core_calls[-1][1]["name"], "my-fleet")

    def test_create_invalid_passes_through_400(self):
        resp = self._run(self._client.post(
            "/teams",
            json={"name": "bad", "roles": {"apex": {"backend": "nope"}}},
            headers=self._auth(),
        ))
        self.assertEqual(resp.status, 400)  # not coerced to 502
        self.assertEqual(self._run(resp.json())["code"], "invalid_team")

    def test_create_bad_json_400_at_gateway(self):
        resp = self._run(self._client.post(
            "/teams", data="not json",
            headers={**self._auth(), "Content-Type": "text/plain"},
        ))
        self.assertEqual(resp.status, 400)
        self.assertEqual(self._run(resp.json())["code"], "invalid_json")

    def test_delete_forwards_and_returns_200(self):
        resp = self._run(self._client.delete("/teams/temp-fleet", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._run(resp.json())["status"], "deleted")
        self.assertEqual(self.core_calls[-1], ("DELETE", "temp-fleet"))

    def test_delete_missing_passes_through_404(self):
        resp = self._run(self._client.delete("/teams/missing", headers=self._auth()))
        self.assertEqual(resp.status, 404)

    def test_backends_forwards_and_returns_payload(self):
        resp = self._run(self._client.get("/backends", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        body = self._run(resp.json())
        self.assertEqual(body["items"][0]["backend"], "local")
        self.assertEqual(body["roles"], ["apex", "worker", "critic"])

    def test_propose_forwards_and_returns_payload(self):
        resp = self._run(self._client.post(
            "/teams/propose", json={"goal": "cheap local"}, headers=self._auth()))
        self.assertEqual(resp.status, 200)
        body = self._run(resp.json())
        self.assertEqual(body["name"], "auto-fleet")
        self.assertEqual(self.core_calls[-1][0], "PROPOSE")

    def test_refine_forwards_and_returns_payload(self):
        resp = self._run(self._client.post(
            "/teams/refine", json={"message": "cheaper worker"}, headers=self._auth()))
        self.assertEqual(resp.status, 200)
        body = self._run(resp.json())
        self.assertEqual(body["reply"], "ok")
        self.assertEqual(self.core_calls[-1][0], "REFINE")

    def test_profiles_list_and_create_forward(self):
        r = self._run(self._client.get("/profiles", headers=self._auth()))
        self.assertEqual(r.status, 200)
        self.assertEqual(self._run(r.json())["items"][0]["name"], "demo-fleet")
        r2 = self._run(self._client.post(
            "/profiles", json={"name": "p", "roles": {}, "shape": "fusion"},
            headers=self._auth()))
        self.assertEqual(r2.status, 201)
        self.assertEqual(self.core_calls[-1][0], "POST-profiles")


if __name__ == "__main__":
    unittest.main()
