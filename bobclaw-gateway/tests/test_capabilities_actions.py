"""BoBClaw Gateway — U3 action registry: /capabilities serving + real-op mapping.

Two things this suite proves (SPEC-UI-OVERHAUL §3 / D4, U3 accept criteria 2 & 3):

  * MAPPING — every seed action's ``binding`` points at a REAL existing gateway op. REST
    bindings must match a registered (method, path) on the actual gateway routers; WS bindings
    must match a ``type`` the chat WS handler dispatches on. This is the cross-service check the
    core suite cannot make (it can't see the gateway routes).
  * SERVING — GET /capabilities carries an additive ``actions`` section equal to the core
    registry payload, plus an ``action_count`` in the capabilities summary. Mounted on the same
    minimal app + fake core as test_capabilities_router.
"""
import asyncio
import unittest
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from auth import auth_middleware, create_access_token
from config import config
from core.actions import get_default_registry
from routers.approvals import router as approvals_router
from routers.capabilities import router as capabilities_router
from routers.conversations import router as conversations_router
from routers.memory import router as memory_router
from routers.teams import router as teams_router

_CHAT_SRC = (Path(__file__).resolve().parents[1] / "routers" / "chat.py").read_text(encoding="utf-8")


def _rest_routes(*routers) -> set:
    """Collect every registered ``(METHOD, path)`` across the given RouteTableDef routers."""
    pairs = set()
    for r in routers:
        for route_def in r:
            method = getattr(route_def, "method", None)
            path = getattr(route_def, "path", None)
            if method and path:
                pairs.add((method.upper(), path))
    return pairs


# ── fake-core payloads (mirrors test_capabilities_router) ───────────────────────
_FACES = [{"id": "assistant", "name": "Assistant", "avatar": "R",
           "preferred_backend": "claude_api", "ui_theme": "grey"}]
_BACKENDS = {"items": [{"backend": "local", "max_usd_per_worker": 0.0, "max_fanout_width": None}],
             "roles": ["apex", "worker", "critic"]}
_MODELS = [{"backend": "local", "available": True, "model": None}]


class TestActionRealOpMapping(unittest.TestCase):
    """U3 accept #3 — each seed action id maps to a real existing REST op / WS tool."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.routes = _rest_routes(
            teams_router, memory_router, conversations_router, approvals_router
        )
        cls.actions = get_default_registry().list_actions()

    def test_seed_set_matches_spec(self):
        self.assertEqual(
            {a.id for a in self.actions},
            {"create_team", "delete_team", "pin_face", "switch_profile",
             "forget_fact", "new_conversation", "approve", "deny"},
        )

    def test_every_rest_binding_hits_a_real_route(self):
        rest = [a for a in self.actions if a.binding.kind == "rest"]
        self.assertTrue(rest)
        for a in rest:
            key = (a.binding.method, a.binding.path)
            self.assertIn(
                key, self.routes,
                f"action {a.id!r} binds {key} which is not a registered gateway route",
            )

    def test_every_ws_binding_hits_a_real_handler(self):
        ws = [a for a in self.actions if a.binding.kind == "ws"]
        self.assertTrue(ws)
        for a in ws:
            needle = f'message_type == "{a.binding.ws_type}"'
            self.assertIn(
                needle, _CHAT_SRC,
                f"action {a.id!r} binds WS type {a.binding.ws_type!r} with no chat handler",
            )

    def test_approve_deny_share_decide_op_with_distinct_decisions(self):
        by_id = {a.id: a for a in self.actions}
        self.assertEqual(by_id["approve"].binding.fixed_params, {"decision": "approve"})
        self.assertEqual(by_id["deny"].binding.fixed_params, {"decision": "reject"})
        self.assertEqual(by_id["approve"].binding.path, by_id["deny"].binding.path)


class TestCapabilitiesServesActions(unittest.TestCase):
    """U3 accept #2 — /capabilities serves the additive actions section."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            async def core_faces(_r):
                return web.json_response(_FACES)

            async def core_backends(_r):
                return web.json_response(_BACKENDS)

            async def core_models(_r):
                return web.json_response(_MODELS)

            core_app = web.Application()
            core_app.router.add_get("/api/faces", core_faces)
            core_app.router.add_get("/api/backends", core_backends)
            core_app.router.add_get("/api/models/available", core_models)
            cls._core_server = TestServer(core_app)
            await cls._core_server.start_server()

            cls._original_core_url = config.CORE_URL
            config.CORE_URL = str(cls._core_server.make_url("/")).rstrip("/")

            app = web.Application(middlewares=[auth_middleware])
            app.router.add_routes(capabilities_router)
            cls._client = TestClient(TestServer(app))
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

    def _get(self):
        headers = {"Authorization": f"Bearer {create_access_token('bobclaw')}"}
        resp = self._loop.run_until_complete(self._client.get("/capabilities", headers=headers))
        body = self._loop.run_until_complete(resp.json())
        return resp, body

    def test_actions_section_equals_registry_payload(self):
        resp, body = self._get()
        self.assertEqual(resp.status, 200)
        self.assertIn("actions", body)
        self.assertEqual(body["actions"], get_default_registry().as_payload())

    def test_action_count_in_capabilities_summary(self):
        _resp, body = self._get()
        self.assertEqual(body["capabilities"]["action_count"], len(body["actions"]))
        self.assertEqual(len(body["actions"]), len(get_default_registry()))

    def test_each_served_action_has_full_schema(self):
        _resp, body = self._get()
        keys = {"id", "title", "description_plain", "params_schema",
                "risk", "undo_hint", "page_scope", "binding"}
        for entry in body["actions"]:
            self.assertTrue(keys.issubset(entry), entry.get("id"))

    def test_no_actions_warning_when_registry_loads(self):
        _resp, body = self._get()
        for w in body.get("warnings", []):
            self.assertFalse(w.startswith("actions:"), w)


if __name__ == "__main__":
    unittest.main()
