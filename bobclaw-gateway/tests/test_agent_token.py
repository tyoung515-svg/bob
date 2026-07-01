"""
Tests for Neck Beard MODE — scoped, default-deny agent bearer tokens.

Security matrix (negative tests lead — the claim lives in the red paths):
 - JWT: create_agent_token carries token_type='agent' + scope + faces and
   round-trips through the unchanged decode_access_token; a human access token
   has NO token_type claim (no regression to the human path).
 - Middleware default-deny: an agent token reaches ONLY the minimal conversation
   endpoints; every admin route, the destructive conversation verbs, and unknown
   routes fail closed (403). A human token is unaffected.
 - Mint endpoint POST /auth/agent-token: admin-authed in-handler; an agent token
   cannot mint (no privilege escalation); malformed scope/faces → 400; no/invalid
   admin token → 401.
 - WS gating: an agent token is rejected on /ws/approvals (both auth patterns)
   and accepted on /ws/chat.
"""
import asyncio
import json
import unittest

import jwt
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import (
    auth_middleware,
    create_access_token,
    create_agent_token,
    decode_access_token,
)
from config import config
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool


_SCOPE = {"may_touch": ["core/teams.py"], "auto_actions": ["cc_edit"], "budget_usd": 1.0}
_FACES = ["assistant", "council-max"]


# ---------------------------------------------------------------------------
# JWT claims
# ---------------------------------------------------------------------------


class TestAgentTokenJWT(unittest.TestCase):
    def test_agent_token_round_trips_through_decode(self):
        token = create_agent_token("admin", scope=_SCOPE, faces=_FACES)
        payload = decode_access_token(token)  # the UNCHANGED verify path
        self.assertIsNotNone(payload)
        self.assertEqual(payload["token_type"], "agent")
        self.assertEqual(payload["scope"], _SCOPE)
        self.assertEqual(payload["faces"], _FACES)
        self.assertEqual(payload["sub"], "admin")

    def test_agent_token_defaults_are_empty_not_none(self):
        payload = decode_access_token(create_agent_token("admin"))
        self.assertEqual(payload["scope"], {})
        self.assertEqual(payload["faces"], [])

    def test_agent_token_carries_unique_jti(self):
        a = decode_access_token(create_agent_token("admin"))
        b = decode_access_token(create_agent_token("admin"))
        self.assertIn("jti", a)
        self.assertNotEqual(a["jti"], b["jti"])  # revocation handle (Phase 5)

    def test_human_token_has_no_token_type_claim(self):
        """Regression guard: the human access token must be unchanged — absence
        of token_type is what grants full access in the middleware."""
        payload = decode_access_token(create_access_token("admin"))
        self.assertNotIn("token_type", payload)

    def test_agent_token_signed_with_same_secret(self):
        # The agent token uses the same HS256 secret/verify path — a token forged
        # with any other secret must not validate (no separate signing snuck in).
        self.assertIsNotNone(decode_access_token(create_agent_token("admin", scope=_SCOPE)))
        forged = jwt.encode(
            {"sub": "x", "token_type": "agent"},
            "a-different-secret-of-sufficient-length-0123456789",
            algorithm="HS256",
        )
        self.assertIsNone(decode_access_token(forged))


# ---------------------------------------------------------------------------
# Middleware default-deny (minimal app: only auth_middleware, no DB)
# ---------------------------------------------------------------------------


def _make_deny_app() -> web.Application:
    app = web.Application(middlewares=[auth_middleware])

    async def ok(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "user": request["user"]["sub"]})

    # Real conversation route shapes (canonical templates matter for matching).
    app.router.add_post("/conversations", ok)
    app.router.add_get("/conversations/{conv_id}", ok)
    app.router.add_get("/conversations/{conv_id}/messages", ok)
    app.router.add_delete("/conversations/{conv_id}", ok)  # destructive → deny
    app.router.add_post("/conversations/{conv_id}/rename", ok)  # admin-ish → deny
    # Representative admin route → deny.
    app.router.add_get("/teams", ok)
    return app


class TestAgentTokenMiddleware(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup():
            cls._client = TestClient(TestServer(_make_deny_app()))
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _agent_hdr(self):
        return {"Authorization": f"Bearer {create_agent_token('admin', scope=_SCOPE)}"}

    def _human_hdr(self):
        return {"Authorization": f"Bearer {create_access_token('admin')}"}

    # -- agent token: DENIED on everything outside the allowlist --
    def test_agent_denied_on_admin_route(self):
        resp = self._run(self._client.get("/teams", headers=self._agent_hdr()))
        self.assertEqual(resp.status, 403)

    def test_agent_denied_on_destructive_conversation_delete(self):
        resp = self._run(self._client.delete("/conversations/abc", headers=self._agent_hdr()))
        self.assertEqual(resp.status, 403)

    def test_agent_denied_on_conversation_rename(self):
        resp = self._run(self._client.post("/conversations/abc/rename", headers=self._agent_hdr()))
        self.assertEqual(resp.status, 403)

    def test_agent_denied_on_unknown_route_fails_closed(self):
        resp = self._run(self._client.get("/nonexistent", headers=self._agent_hdr()))
        self.assertEqual(resp.status, 403)

    def test_agent_denied_wrong_method_on_allowed_path(self):
        # GET /conversations is NOT in the allowlist (only POST is).
        resp = self._run(self._client.get("/conversations", headers=self._agent_hdr()))
        self.assertEqual(resp.status, 403)

    # -- agent token: ALLOWED on the minimal conversation surface --
    def test_agent_allowed_create_conversation(self):
        resp = self._run(self._client.post("/conversations", headers=self._agent_hdr()))
        self.assertEqual(resp.status, 200)

    def test_agent_allowed_get_conversation(self):
        resp = self._run(self._client.get("/conversations/abc", headers=self._agent_hdr()))
        self.assertEqual(resp.status, 200)

    def test_agent_allowed_get_messages(self):
        resp = self._run(self._client.get("/conversations/abc/messages", headers=self._agent_hdr()))
        self.assertEqual(resp.status, 200)

    # -- human token: UNAFFECTED (no regression) --
    def test_human_allowed_on_admin_route(self):
        resp = self._run(self._client.get("/teams", headers=self._human_hdr()))
        self.assertEqual(resp.status, 200)

    def test_human_allowed_on_destructive_delete(self):
        resp = self._run(self._client.delete("/conversations/abc", headers=self._human_hdr()))
        self.assertEqual(resp.status, 200)

    def test_human_unknown_route_is_404_not_403(self):
        resp = self._run(self._client.get("/nonexistent", headers=self._human_hdr()))
        self.assertEqual(resp.status, 404)


# ---------------------------------------------------------------------------
# Mint endpoint POST /auth/agent-token (full app; in-handler admin auth)
# ---------------------------------------------------------------------------


class TestAgentTokenMint(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup():
            pool = InMemoryPostgresPool()
            cls._client = TestClient(TestServer(build_app({POSTGRES_POOL_KEY: pool})))
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _post(self, headers=None, body=None):
        return self._run(
            self._client.post("/auth/agent-token", headers=headers or {}, json=body or {})
        )

    # -- negatives first --
    def test_mint_requires_authorization(self):
        resp = self._post(body={"scope": _SCOPE})
        self.assertEqual(resp.status, 401)

    def test_mint_rejects_invalid_admin_token(self):
        resp = self._post(headers={"Authorization": "Bearer not.a.token"}, body={"scope": _SCOPE})
        self.assertEqual(resp.status, 401)

    def test_agent_token_cannot_mint_agent_token(self):
        """No privilege escalation: an agent bearer cannot mint more agents."""
        agent = create_agent_token("admin", scope=_SCOPE)
        resp = self._post(headers={"Authorization": f"Bearer {agent}"}, body={"scope": _SCOPE})
        self.assertEqual(resp.status, 403)

    def test_mint_rejects_malformed_scope(self):
        admin = create_access_token("admin")
        resp = self._post(
            headers={"Authorization": f"Bearer {admin}"},
            body={"scope": {"may_touch": "not-a-list"}},
        )
        self.assertEqual(resp.status, 400)

    def test_mint_rejects_non_list_faces(self):
        admin = create_access_token("admin")
        resp = self._post(
            headers={"Authorization": f"Bearer {admin}"},
            body={"scope": _SCOPE, "faces": "assistant"},
        )
        self.assertEqual(resp.status, 400)

    def test_mint_rejects_too_many_faces(self):
        admin = create_access_token("admin")
        resp = self._post(
            headers={"Authorization": f"Bearer {admin}"},
            body={"scope": _SCOPE, "faces": [f"f{i}" for i in range(33)]},
        )
        self.assertEqual(resp.status, 400)

    def test_mint_rejects_unknown_scope_key(self):
        """A typo'd/unknown scope key must 400, not silently drop (extra='ignore')."""
        admin = create_access_token("admin")
        resp = self._post(
            headers={"Authorization": f"Bearer {admin}"},
            body={"scope": {"may_touch": ["x"], "bogus_key": 1}},
        )
        self.assertEqual(resp.status, 400)

    # -- happy path --
    def test_mint_returns_scoped_agent_token(self):
        admin = create_access_token("admin")
        resp = self._post(
            headers={"Authorization": f"Bearer {admin}"},
            body={"scope": _SCOPE, "faces": _FACES},
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        payload = decode_access_token(data["access_token"])
        self.assertEqual(payload["token_type"], "agent")
        self.assertEqual(payload["scope"]["may_touch"], ["core/teams.py"])
        self.assertEqual(payload["faces"], _FACES)
        self.assertIn("jti", payload)  # revocation handle for Phase 5

    def test_minted_token_is_denied_on_admin_route_through_real_app(self):
        """End-to-end: a freshly minted agent token is correctly marked and the
        real middleware denies it on an admin router (here /faces) with 403,
        before the handler/proxy ever runs."""
        admin = create_access_token("admin")
        resp = self._post(headers={"Authorization": f"Bearer {admin}"}, body={"scope": _SCOPE})
        token = self._run(resp.json())["access_token"]
        denied = self._run(self._client.get("/faces", headers={"Authorization": f"Bearer {token}"}))
        self.assertEqual(denied.status, 403)


# ---------------------------------------------------------------------------
# WS gating: agent rejected on /ws/approvals, accepted on /ws/chat
# ---------------------------------------------------------------------------


class TestAgentTokenWebSocket(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup():
            pool = InMemoryPostgresPool()
            cls._client = TestClient(TestServer(build_app({POSTGRES_POOL_KEY: pool})))
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _agent(self):
        return create_agent_token("admin", scope=_SCOPE, faces=_FACES)

    def test_agent_rejected_on_approvals_header_pattern(self):
        ws = self._run(
            self._client.ws_connect(
                "/ws/approvals", headers={"Authorization": f"Bearer {self._agent()}"}
            )
        )
        msg = self._run(ws.receive())
        self.assertEqual(msg.type, WSMsgType.TEXT)
        self.assertEqual(json.loads(msg.data)["code"], "forbidden")
        self._run(ws.close())

    def test_agent_rejected_on_approvals_first_frame_pattern(self):
        ws = self._run(self._client.ws_connect("/ws/approvals"))
        self._run(ws.send_json({"type": "auth", "token": self._agent()}))
        msg = self._run(ws.receive())
        # Pattern-2 rejection closes with code 4401 (no error frame).
        self.assertIn(msg.type, {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED})
        self._run(ws.close())

    def test_agent_accepted_on_chat(self):
        """allow_agent=True path: an agent token authenticates on /ws/chat AND the
        server stays in its receive loop. Positive proof via a round-trip (a bare
        assertFalse(ws.closed) is racy — a not-yet-delivered close reads as open)."""
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat", headers={"Authorization": f"Bearer {self._agent()}"}
            )
        )
        # An unknown conversation yields a 'not_found' error frame WITHOUT closing,
        # proving auth passed and the post-auth loop is running.
        self._run(ws.send_json({"type": "message", "conversation_id": "nope", "content": "x"}))
        msg = self._run(asyncio.wait_for(ws.receive(), timeout=5))
        self.assertEqual(msg.type, WSMsgType.TEXT)
        self.assertEqual(json.loads(msg.data).get("code"), "not_found")
        self._run(ws.close())

    def test_agent_cannot_submit_approval_response(self):
        """P3: an agent on /ws/chat must NOT self-approve a parked destructive action.
        An approval_response frame from an agent token is rejected ('forbidden') and
        never forwarded to core /api/chat/approval (closing the deferred P1/P2 finding)."""
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat", headers={"Authorization": f"Bearer {self._agent()}"}
            )
        )
        self._run(ws.send_json({
            "type": "approval_response", "approval_id": "deadbeef", "decision": "approve",
        }))
        msg = self._run(asyncio.wait_for(ws.receive(), timeout=5))
        self.assertEqual(msg.type, WSMsgType.TEXT)
        self.assertEqual(json.loads(msg.data).get("code"), "forbidden")
        self._run(ws.close())

    def test_non_dict_first_frame_closes_not_crash(self):
        """A valid-JSON non-object first frame (e.g. '[]') must close 4401, not
        raise an AttributeError out of authenticate_ws. Pre-auth on BOTH WS endpoints."""
        for endpoint in ("/ws/chat", "/ws/approvals"):
            ws = self._run(self._client.ws_connect(endpoint))
            self._run(ws.send_str("[]"))  # valid JSON, not an object
            msg = self._run(asyncio.wait_for(ws.receive(), timeout=5))
            self.assertIn(msg.type, {WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED})
            self._run(ws.close())
