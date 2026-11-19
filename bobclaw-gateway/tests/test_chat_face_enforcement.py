"""F1 fix tests (2026-09-30): the gateway enforces the agent token's ``faces``
allow-list SERVER-SIDE on /ws/chat.

Before the fix, face-scoping existed only in the MCP client — any agent token
could reach ANY face over the WS (verified live against the production
gateway). These tests pin the new contract:

* an agent token with faces=['assistant'] CANNOT send a message that targets
  a face outside the list (error frame ``face_forbidden``, no core call);
* it also cannot PIN such a face via switch_face;
* it CAN still use a listed face;
* an agent token with an absent/empty faces claim stays unrestricted
  (existing semantics — single-purpose tokens keep working);
* human tokens are never restricted.
"""
import asyncio
import json
import unittest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token, create_agent_token
from config import config
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool


class TestAgentFaceEnforcementWS(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient
    _core_server: TestServer
    _pool: InMemoryPostgresPool
    _original_core_url: str
    core_requests: list[dict]

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            cls.core_requests = []

            async def core_chat(request: web.Request) -> web.StreamResponse:
                body = await request.json()
                cls.core_requests.append(body)
                response = web.StreamResponse(headers={"Content-Type": "application/json"})
                await response.prepare(request)
                events = [
                    {"type": "chunk", "content": "ok", "model": body.get("model") or "m", "backend": "core"},
                    {"type": "message_complete", "message_id": "assistant-1", "tokens_in": 1, "tokens_out": 1, "elapsed_ms": 1},
                ]
                for event in events:
                    await response.write((json.dumps(event) + "\n").encode())
                await response.write_eof()
                return response

            core_app = web.Application()
            core_app.router.add_post("/api/chat", core_chat)
            cls._core_server = TestServer(core_app)
            await cls._core_server.start_server()

            cls._original_core_url = config.CORE_URL
            config.CORE_URL = str(cls._core_server.make_url("/")).rstrip("/")

            cls._pool = InMemoryPostgresPool()
            cls._pool.add_conversation(title="Agent Face Conv", conv_id="conv-af", user_id="admin")
            app = build_app()
            app[POSTGRES_POOL_KEY] = cls._pool
            gateway_server = TestServer(app)
            await gateway_server.start_server()
            cls._client = TestClient(gateway_server)

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.run_until_complete(cls._core_server.close())
        config.CORE_URL = cls._original_core_url
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _ws_turn(self, token: str, message: dict) -> tuple[list[dict], int]:
        """Open the WS, send one message, collect frames until a terminal frame
        or close. Returns (frames, n_core_calls_snapshot_delta)."""
        before = len(self.core_requests)

        async def _do() -> list[dict]:
            async with self._client.ws_connect(
                "/ws/chat", headers={"Authorization": f"Bearer {token}"}
            ) as ws:
                await ws.send_json(message)
                frames = []
                async for msg in ws:
                    if msg.type != WSMsgType.TEXT:
                        break
                    evt = json.loads(msg.data)
                    frames.append(evt)
                    if evt.get("type") in ("message_complete", "error", "generation_stopped"):
                        break
                return frames

        frames = self._run(_do())
        return frames, len(self.core_requests) - before

    def test_agent_token_cannot_message_unlisted_face(self):
        token = create_agent_token("admin", scope={}, faces=["assistant"])
        frames, calls = self._ws_turn(
            token,
            {"type": "message", "conversation_id": "conv-af", "content": "hi", "face_id": "planner-gpt56-sol"},
        )
        errors = [f for f in frames if f.get("type") == "error" and f.get("code") == "face_forbidden"]
        self.assertTrue(errors, f"expected face_forbidden error frame, got: {frames}")
        self.assertEqual(calls, 0, "core must NOT be called for a denied face")

    def test_agent_token_cannot_pin_unlisted_face(self):
        token = create_agent_token("admin", scope={}, faces=["assistant"])
        frames, _ = self._ws_turn(
            token,
            {"type": "switch_face", "conversation_id": "conv-af", "face_id": "reviewer"},
        )
        errors = [f for f in frames if f.get("type") == "error" and f.get("code") == "face_forbidden"]
        self.assertTrue(errors, f"expected face_forbidden on switch_face, got: {frames}")

    def test_agent_token_can_use_listed_face(self):
        token = create_agent_token("admin", scope={}, faces=["assistant"])
        frames, calls = self._ws_turn(
            token,
            {"type": "message", "conversation_id": "conv-af", "content": "hi", "face_id": "assistant"},
        )
        self.assertEqual(calls, 1, "a listed face must reach core")
        self.assertTrue(
            any(f.get("type") == "message_complete" for f in frames),
            f"expected message_complete, got: {frames}",
        )

    def test_agent_token_with_empty_faces_stays_unrestricted(self):
        # Existing semantics preserved: an agent token with no faces claim can
        # pick any face (single-purpose/legacy tokens keep working).
        token = create_agent_token("admin", scope={}, faces=[])
        frames, calls = self._ws_turn(
            token,
            {"type": "message", "conversation_id": "conv-af", "content": "hi", "face_id": "reviewer"},
        )
        self.assertEqual(calls, 1, "unrestricted agent token must reach core")
        self.assertTrue(any(f.get("type") == "message_complete" for f in frames), f"{frames}")

    def test_human_token_unrestricted(self):
        token = create_access_token("admin")
        frames, calls = self._ws_turn(
            token,
            {"type": "message", "conversation_id": "conv-af", "content": "hi", "face_id": "reviewer"},
        )
        self.assertEqual(calls, 1, "human token must never be face-restricted")
        self.assertTrue(any(f.get("type") == "message_complete" for f in frames), f"{frames}")

    def test_denied_face_does_not_leak_concurrency_slot(self):
        """2026-10-01: a face_forbidden denial must RELEASE the reserved
        active_streams slot — no stream task is launched, so without an
        explicit discard the slot leaked for the process lifetime. Fire more
        denials than the per-user cap, then prove a listed face still streams
        (a leaked slot would surface as concurrency_limit)."""
        cap = config.MAX_CONCURRENT_STREAMS_PER_USER
        for i in range(cap + 2):
            conv_id = f"conv-deny-{i}"
            self._pool.add_conversation(title="deny", conv_id=conv_id, user_id="admin")
            token = create_agent_token("admin", scope={}, faces=["assistant"])
            frames, calls = self._ws_turn(
                token,
                {"type": "message", "conversation_id": conv_id, "content": "hi", "face_id": "planner-gpt56-sol"},
            )
            self.assertTrue(
                any(f.get("type") == "error" and f.get("code") == "face_forbidden" for f in frames),
                f"denial {i}: expected face_forbidden, got {frames}",
            )
            self.assertEqual(calls, 0)
        # If any denial leaked a slot, the cap would now be exhausted.
        token = create_agent_token("admin", scope={}, faces=["assistant"])
        frames, calls = self._ws_turn(
            token,
            {"type": "message", "conversation_id": "conv-af", "content": "hi", "face_id": "assistant"},
        )
        self.assertEqual(calls, 1, "listed face must still reach core after >cap denials")
        self.assertTrue(
            any(f.get("type") == "message_complete" for f in frames),
            f"expected message_complete (not concurrency_limit), got: {frames}",
        )

    def test_agent_token_denied_via_conversation_stored_pin(self):
        # The conversation's STORED face pin (DB row) is also checked: an
        # agent token scoped to 'assistant' cannot take over a conversation
        # whose stored pin is an unlisted face by omitting face_id.
        self._run(
            self._pool.execute(
                "UPDATE conversations SET face_id = $2, updated_at = NOW() WHERE id = $1 AND user_id = $3",
                "conv-af", "planner-gpt56-sol", "admin",
            )
        )
        token = create_agent_token("admin", scope={}, faces=["assistant"])
        frames, calls = self._ws_turn(
            token,
            {"type": "message", "conversation_id": "conv-af", "content": "hi"},
        )
        errors = [f for f in frames if f.get("type") == "error" and f.get("code") == "face_forbidden"]
        self.assertTrue(errors, f"expected face_forbidden via stored pin, got: {frames}")
        self.assertEqual(calls, 0, "core must NOT be called")
        # restore
        self._run(
            self._pool.execute(
                "UPDATE conversations SET face_id = NULL, updated_at = NOW() WHERE id = $1 AND user_id = $2",
                "conv-af", "admin",
            )
        )


if __name__ == "__main__":
    unittest.main()
