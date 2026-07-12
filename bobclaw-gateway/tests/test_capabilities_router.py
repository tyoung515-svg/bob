"""BoBClaw Gateway — /capabilities registry endpoint tests (MS8-G1).

Verifies GET /capabilities requires a JWT, aggregates core's three read surfaces
(/api/faces + /api/backends + /api/models/available) into ONE read-only document,
merges backends by name (availability + cost caps), degrades a partial core outage to
a warnings list (still 200), surfaces 502 only when the whole core registry is down,
and issues GETs only (read-only).

The router is mounted on a MINIMAL app (auth_middleware + the capabilities router) so the
endpoint is exercised end-to-end without wiring it into gateway.build_app() — the conductor
adds that one-line wiring at assembly (see RESULTS-G1 shared_file_requests).
"""
import asyncio
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import auth_middleware, create_access_token
from config import config
from routers.capabilities import router as capabilities_router

# ── fixed fake-core payloads ─────────────────────────────────────────────────
# builder-bob carries U2 display metadata (present-path); assistant omits it
# entirely (absent-path — the endpoint must null-fill the three keys).
_FACES = [
    {"id": "builder-bob", "name": "Builder Bob", "avatar": "H",
     "preferred_backend": "local", "ui_theme": "blue",
     "display_name": "Builder Bob", "blurb": "Turns ideas into working code.",
     "simple_slot": "quick"},
    {"id": "assistant", "name": "Assistant", "avatar": "R",
     "preferred_backend": "claude_api", "ui_theme": "grey"},
]

# U2 display keys the endpoint guarantees on every faces[] entry.
_DISPLAY_KEYS = ("display_name", "blurb", "simple_slot")


def _with_display(faces: list) -> list:
    """Expected /capabilities faces[]: every entry carries the three display keys
    (present ⇒ verbatim, absent ⇒ null)."""
    return [{**f, **{k: f.get(k) for k in _DISPLAY_KEYS}} for f in faces]
_BACKENDS = {
    "items": [
        {"backend": "deepseek_v4_flash", "max_usd_per_worker": 0.5, "max_fanout_width": 8},
        {"backend": "minimax", "max_usd_per_worker": 0.3, "max_fanout_width": 4},
        {"backend": "local", "max_usd_per_worker": 0.0, "max_fanout_width": None},
    ],
    "roles": ["apex", "worker", "critic"],
}
_MODELS = [
    {"backend": "local", "available": True, "model": None, "models": ["gemma"]},
    {"backend": "deepseek_v4_flash", "available": True, "model": "deepseek-chat"},
    {"backend": "claude_api", "available": False, "model": "claude-x"},
]


def _make_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])
    app.router.add_routes(capabilities_router)
    return app


class TestCapabilitiesEndpoint(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient
    _core_server: TestServer
    _original_core_url: str

    # Per-test knobs (reset in setUp): status each fake-core endpoint returns.
    faces_status: int = 200
    backends_status: int = 200
    models_status: int = 200
    core_methods: list

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            cls.core_methods = []

            async def core_faces(request: web.Request) -> web.Response:
                cls.core_methods.append(request.method)
                if cls.faces_status != 200:
                    return web.json_response({"error": "boom"}, status=cls.faces_status)
                return web.json_response(_FACES)

            async def core_backends(request: web.Request) -> web.Response:
                cls.core_methods.append(request.method)
                if cls.backends_status != 200:
                    return web.json_response({"error": "boom"}, status=cls.backends_status)
                return web.json_response(_BACKENDS)

            async def core_models(request: web.Request) -> web.Response:
                cls.core_methods.append(request.method)
                if cls.models_status != 200:
                    return web.json_response({"error": "boom"}, status=cls.models_status)
                return web.json_response(_MODELS)

            core_app = web.Application()
            core_app.router.add_get("/api/faces", core_faces)
            core_app.router.add_get("/api/backends", core_backends)
            core_app.router.add_get("/api/models/available", core_models)
            cls._core_server = TestServer(core_app)
            await cls._core_server.start_server()

            cls._original_core_url = config.CORE_URL
            config.CORE_URL = str(cls._core_server.make_url("/")).rstrip("/")

            cls._client = TestClient(TestServer(_make_app()))
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
        type(self).faces_status = 200
        type(self).backends_status = 200
        type(self).models_status = 200
        type(self).core_methods.clear()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth(self):
        return {"Authorization": f"Bearer {create_access_token('bobclaw')}"}

    # ── auth ────────────────────────────────────────────────────────────────
    def test_requires_jwt(self):
        resp = self._run(self._client.get("/capabilities"))
        self.assertEqual(resp.status, 401)

    # ── full aggregation shape ──────────────────────────────────────────────
    def test_aggregates_faces_backends_capabilities(self):
        resp = self._run(self._client.get("/capabilities", headers=self._auth()))
        self.assertEqual(resp.status, 200)
        body = self._run(resp.json())

        # faces passed through, with the three display keys guaranteed per entry
        self.assertEqual(body["faces"], _with_display(_FACES))

        # capabilities summary
        caps = body["capabilities"]
        self.assertEqual(caps["roles"], ["apex", "worker", "critic"])
        self.assertEqual(caps["face_count"], 2)
        self.assertEqual(caps["backend_count"], 4)  # union of the two backend sources
        # available_backends = those with available True (sorted)
        self.assertEqual(caps["available_backends"], ["deepseek_v4_flash", "local"])

        # no warnings when all three fetches succeed
        self.assertNotIn("warnings", body)

    def test_backends_merged_by_name(self):
        resp = self._run(self._client.get("/capabilities", headers=self._auth()))
        body = self._run(resp.json())
        by_name = {b["backend"]: b for b in body["backends"]}

        # stable ascending sort by backend name
        self.assertEqual([b["backend"] for b in body["backends"]],
                         ["claude_api", "deepseek_v4_flash", "local", "minimax"])

        # in BOTH sources: availability from models/available + caps from /api/backends
        ds = by_name["deepseek_v4_flash"]
        self.assertEqual(
            (ds["available"], ds["model"], ds["max_usd_per_worker"], ds["max_fanout_width"]),
            (True, "deepseek-chat", 0.5, 8),
        )
        # models/available ONLY (not a cost-capped backend): caps default to null
        ca = by_name["claude_api"]
        self.assertEqual(
            (ca["available"], ca["model"], ca["max_usd_per_worker"], ca["max_fanout_width"]),
            (False, "claude-x", None, None),
        )
        # /api/backends ONLY (not in models/available): available defaults False, caps present
        mm = by_name["minimax"]
        self.assertEqual(
            (mm["available"], mm["model"], mm["max_usd_per_worker"], mm["max_fanout_width"]),
            (False, None, 0.3, 4),
        )
        # in both, caps present with a null width preserved
        lo = by_name["local"]
        self.assertEqual(
            (lo["available"], lo["model"], lo["max_usd_per_worker"], lo["max_fanout_width"]),
            (True, None, 0.0, None),
        )

    # ── U2 display metadata passthrough ──────────────────────────────────────
    def test_faces_carry_display_metadata(self):
        resp = self._run(self._client.get("/capabilities", headers=self._auth()))
        body = self._run(resp.json())
        by_id = {f["id"]: f for f in body["faces"]}

        # present ⇒ passed through verbatim
        bb = by_id["builder-bob"]
        self.assertEqual(bb["display_name"], "Builder Bob")
        self.assertEqual(bb["blurb"], "Turns ideas into working code.")
        self.assertEqual(bb["simple_slot"], "quick")

        # absent ⇒ the three keys are still present, and null (stable client schema)
        asst = by_id["assistant"]
        for key in ("display_name", "blurb", "simple_slot"):
            self.assertIn(key, asst, f"{key} missing on a face that lacked it")
            self.assertIsNone(asst[key])

    # ── read-only ───────────────────────────────────────────────────────────
    def test_read_only_issues_only_gets(self):
        self._run(self._client.get("/capabilities", headers=self._auth()))
        self.assertTrue(self.core_methods)  # core was actually called
        self.assertTrue(all(m == "GET" for m in self.core_methods),
                        f"non-GET method hit core: {self.core_methods}")

    # ── partial degradation ─────────────────────────────────────────────────
    def test_partial_core_outage_degrades_with_warnings(self):
        type(self).backends_status = 500  # /api/backends is down
        resp = self._run(self._client.get("/capabilities", headers=self._auth()))
        self.assertEqual(resp.status, 200)  # still serves — degrade, don't hard-fail
        body = self._run(resp.json())

        # a warning names the failed component
        self.assertIn("warnings", body)
        self.assertTrue(any(w.startswith("backends:") for w in body["warnings"]),
                        body["warnings"])

        # faces + models survived; roles empty (backends supplied them)
        self.assertEqual(body["faces"], _with_display(_FACES))
        self.assertEqual(body["capabilities"]["roles"], [])
        # backends now come only from models/available (no cost caps)
        names = [b["backend"] for b in body["backends"]]
        self.assertEqual(names, ["claude_api", "deepseek_v4_flash", "local"])
        self.assertTrue(all(b["max_usd_per_worker"] is None for b in body["backends"]))

    # ── total outage → 502 ──────────────────────────────────────────────────
    def test_all_core_down_returns_502(self):
        type(self).faces_status = 500
        type(self).backends_status = 500
        type(self).models_status = 500
        resp = self._run(self._client.get("/capabilities", headers=self._auth()))
        self.assertEqual(resp.status, 502)
        self.assertEqual(resp.content_type, "application/json")
        body = self._run(resp.json())
        self.assertIn("unreachable", body["error"])


if __name__ == "__main__":
    unittest.main()
