import asyncio
import json
import unittest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from app_state import CONVERSATION_STATE_KEY, POSTGRES_POOL_KEY
from auth import create_access_token
from config import config
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool


class TestChatWebSocket(unittest.TestCase):
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
                if "RAISE-STATE-ERROR" in (body.get("content") or ""):
                    # Simulate core emitting a structured error event (e.g.
                    # route_node rejecting an unknown backend) before the
                    # empty completion.
                    events = [
                        {"type": "error", "message": "Unknown backend 'minimx'", "code": "state_error"},
                        {"type": "message_complete", "message_id": "assistant-err", "tokens_in": 0, "tokens_out": 0, "elapsed_ms": 1},
                    ]
                else:
                    events = [
                        {"type": "chunk", "content": "Hello", "model": body.get("model") or "gpt-5", "backend": body.get("backend") or "core"},
                        {"type": "chunk", "content": " world", "model": body.get("model") or "gpt-5", "backend": body.get("backend") or "core"},
                        {"type": "message_complete", "message_id": "assistant-1", "tokens_in": 4, "tokens_out": 2, "elapsed_ms": 12},
                    ]
                for event in events:
                    await response.write((json.dumps(event) + "\n").encode())
                await response.write_eof()
                return response

            async def approval(request: web.Request) -> web.Response:
                return web.json_response({"status": "ok"})

            core_app = web.Application()
            core_app.router.add_post("/api/chat", core_chat)
            core_app.router.add_post("/api/chat/approval", approval)
            cls._core_server = TestServer(core_app)
            await cls._core_server.start_server()

            cls._original_core_url = config.CORE_URL
            config.CORE_URL = str(cls._core_server.make_url("/")).rstrip("/")

            cls._pool = InMemoryPostgresPool()
            cls._pool.add_conversation(title="WS Conversation", conv_id="conv-ws", user_id="bobclaw")
            cls._pool.add_conversation(title="Other Conversation", conv_id="conv-other", user_id="bobclaw")
            cls._client = TestClient(
                TestServer(build_app({POSTGRES_POOL_KEY: cls._pool}))
            )
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
        self._pool.messages.clear()
        self.core_requests.clear()
        self._client.server.app[CONVERSATION_STATE_KEY].clear()
        # switch_face / switch_model now PERSIST the pin to the conversation row
        # (so "Auto" can clear an inherited project default). The pool is
        # class-scoped, so reset each conversation's pins to the unpinned
        # baseline between tests to keep them isolated.
        for conv in self._pool.conversations.values():
            conv["face_id"] = None
            conv["backend_preference"] = None
            conv["model_preference"] = None

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _token(self):
        return create_access_token("bobclaw")

    def test_websocket_connection_with_valid_header_token(self):
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(ws.close())
        self.assertTrue(ws.closed)

    def test_websocket_rejects_invalid_header_token(self):
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": "Bearer not-a-valid-token"},
            )
        )
        msg = self._run(ws.receive())
        self.assertEqual(msg.type, WSMsgType.TEXT)
        data = json.loads(msg.data)
        self.assertEqual(data["code"], "invalid_token")
        self._run(ws.close())

    def test_ws_approval_response_core_down_sends_error_frame(self):
        """If core is unreachable, an approval_response must surface an error frame
        (code=approval_error) instead of letting the exception bubble out of
        _handle_message and silently kill the socket."""
        from unittest.mock import patch
        import aiohttp as _aiohttp

        class _BoomSession:
            def __init__(self, *a, **k):
                raise _aiohttp.ClientConnectionError("core down")

        ws = self._run(
            self._client.ws_connect(
                "/ws/chat", headers={"Authorization": f"Bearer {self._token()}"}
            )
        )
        with patch("routers.chat.aiohttp.ClientSession", _BoomSession):
            self._run(ws.send_json({
                "type": "approval_response",
                "approval_id": "a1",
                "decision": "approve",
            }))
            msg = self._run(ws.receive_json())
        self.assertEqual(msg["type"], "error")
        self.assertEqual(msg["code"], "approval_error")
        self.assertFalse(ws.closed)  # socket survived
        self._run(ws.close())

    def test_websocket_first_message_auth(self):
        ws = self._run(self._client.ws_connect("/ws/chat"))
        self._run(ws.send_json({"type": "auth", "token": self._token()}))
        # After auth, send a real message
        self._run(
            ws.send_json(
                {
                    "type": "message",
                    "conversation_id": "conv-ws",
                    "content": "Hi there",
                    "model": "gpt-5",
                }
            )
        )
        first = self._run(ws.receive_json())
        self.assertEqual(first["type"], "chunk")
        self._run(ws.close())

    def test_websocket_rejects_missing_auth(self):
        ws = self._run(self._client.ws_connect("/ws/chat"))
        msg = self._run(ws.receive())
        self.assertEqual(msg.type, WSMsgType.CLOSE)
        self._run(ws.close())

    def test_message_flow(self):
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(
            ws.send_json(
                {
                    "type": "message",
                    "conversation_id": "conv-ws",
                    "content": "Hi there",
                    "model": "gpt-5",
                }
            )
        )

        first = self._run(ws.receive_json())
        second = self._run(ws.receive_json())
        complete = self._run(ws.receive_json())

        self.assertEqual(first["type"], "chunk")
        self.assertEqual(second["type"], "chunk")
        self.assertEqual(complete["type"], "message_complete")
        self.assertEqual([message["role"] for message in self._pool.messages], ["user", "assistant"])
        self.assertEqual(self._pool.messages[-1]["content"], "Hello world")
        self._run(ws.close())

    def test_user_id_sent_to_core_in_upstream_payload(self):
        """The gateway must forward the authenticated user's id to core /api/chat."""
        self.core_requests.clear()
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(
            ws.send_json(
                {
                    "type": "message",
                    "conversation_id": "conv-ws",
                    "content": "Hi there",
                }
            )
        )
        # Wait for the stream to complete.
        for _ in range(3):
            self._run(ws.receive_json())
        self._run(ws.close())

        self.assertEqual(len(self.core_requests), 1)
        self.assertEqual(self.core_requests[0].get("user_id"), "bobclaw")

    def test_upstream_error_event_forwarded_to_client(self):
        """Core 'error' events (e.g. state_error from route_node rejecting an
        unknown backend) must reach the WS client, not be silently dropped."""
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(
            ws.send_json(
                {
                    "type": "message",
                    "conversation_id": "conv-ws",
                    "content": "RAISE-STATE-ERROR please",
                }
            )
        )

        first = self._run(ws.receive_json())
        complete = self._run(ws.receive_json())

        self.assertEqual(first["type"], "error")
        self.assertEqual(first["code"], "state_error")
        self.assertIn("Unknown backend", first["message"])
        self.assertEqual(complete["type"], "message_complete")
        self._run(ws.close())

    def test_face_switching(self):
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(ws.send_json({"type": "switch_face", "face_id": "sage", "conversation_id": "conv-ws"}))
        response = self._run(ws.receive_json())
        self.assertEqual(response["type"], "face_switched")
        self.assertEqual(response["face_id"], "sage")
        self._run(ws.close())

    def test_switch_model_backend_only_pin_allowed(self):
        """model is optional in switch_model: backend-only pin stores model=None
        so the backend's own selection (e.g. local resident pick) decides."""
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(ws.send_json({"type": "switch_model", "backend": "local", "conversation_id": "conv-ws"}))
        response = self._run(ws.receive_json())
        self.assertEqual(response["type"], "model_switched")
        self.assertIsNone(response["model"])
        self.assertEqual(response["backend"], "local")
        self._run(ws.close())

    def test_switch_face_empty_clears_pin(self):
        """Empty face_id un-pins the conversation (UI 'Auto'); acks with nulls,
        does NOT error."""
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        # pin then clear
        self._run(ws.send_json({"type": "switch_face", "face_id": "sage", "conversation_id": "conv-ws"}))
        self._run(ws.receive_json())
        self._run(ws.send_json({"type": "switch_face", "face_id": "", "conversation_id": "conv-ws"}))
        response = self._run(ws.receive_json())
        self.assertEqual(response["type"], "face_switched")
        self.assertIsNone(response["face_id"])
        self.assertIsNone(response["face_name"])
        self._run(ws.close())

    def test_switch_model_empty_backend_clears_pin(self):
        """Empty backend un-pins the conversation (UI 'Auto'); acks with nulls,
        does NOT error."""
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(ws.send_json({"type": "switch_model", "backend": "local", "conversation_id": "conv-ws"}))
        self._run(ws.receive_json())
        self._run(ws.send_json({"type": "switch_model", "backend": "", "conversation_id": "conv-ws"}))
        response = self._run(ws.receive_json())
        self.assertEqual(response["type"], "model_switched")
        self.assertIsNone(response["backend"])
        self.assertIsNone(response["model"])
        self._run(ws.close())

    def test_switch_face_rejects_missing_conversation_id(self):
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(ws.send_json({"type": "switch_face", "face_id": "sage"}))
        response = self._run(ws.receive_json())
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["code"], "invalid_conversation")
        self._run(ws.close())

    def test_switch_model_rejects_missing_conversation_id(self):
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(ws.send_json({"type": "switch_model", "model": "gpt-5", "backend": "openai"}))
        response = self._run(ws.receive_json())
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["code"], "invalid_conversation")
        self._run(ws.close())

    def test_stop_generation_when_idle(self):
        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(ws.send_json({"type": "stop_generation"}))
        response = self._run(ws.receive_json())
        self.assertEqual(response["type"], "error")
        self.assertEqual(response["code"], "no_active_generation")
        self._run(ws.close())

    def test_stop_generation_during_stream(self):
        """Cancels an in-flight stream using a slow mock core server."""
        from aiohttp import web
        from config import config

        async def slow_core(request: web.Request) -> web.StreamResponse:
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            for i in range(100):
                await response.write(
                    (json.dumps({"type": "chunk", "content": f"word{i} "}) + "\n").encode()
                )
                await asyncio.sleep(0.05)
            await response.write_eof()
            return response

        slow_app = web.Application()
        slow_app.router.add_post("/api/chat", slow_core)
        slow_server = TestServer(slow_app)
        self._run(slow_server.start_server())

        original_core_url = config.CORE_URL
        config.CORE_URL = str(slow_server.make_url("/")).rstrip("/")

        try:
            ws = self._run(
                self._client.ws_connect(
                    "/ws/chat",
                    headers={"Authorization": f"Bearer {self._token()}"},
                )
            )
            self._run(
                ws.send_json(
                    {
                        "type": "message",
                        "conversation_id": "conv-ws",
                        "content": "Keep talking",
                    }
                )
            )
            # Receive first chunk to confirm stream started
            first = self._run(ws.receive_json())
            self.assertEqual(first["type"], "chunk")

            # Stop the generation
            self._run(ws.send_json({"type": "stop_generation"}))
            stopped = self._run(ws.receive_json())
            self.assertEqual(stopped["type"], "generation_stopped")

            # Should still get a message_complete with partial output
            complete = self._run(ws.receive_json())
            self.assertEqual(complete["type"], "message_complete")
            self.assertTrue(len(self._pool.messages) >= 1)
            self.assertTrue(self._pool.messages[-1]["content"].startswith("word0"))
        finally:
            config.CORE_URL = original_core_url
            self._run(slow_server.close())
        self._run(ws.close())

    def test_history_sent_in_upstream_payload_when_messages_exist(self):
        """When a conversation has prior messages, the gateway must include
        them in the upstream payload under the 'history' key."""
        from aiohttp import web
        from config import config

        self._pool.add_conversation(
            title="History Test", conv_id="conv-ws-history", user_id="bobclaw",
        )
        self._pool.add_message(
            conversation_id="conv-ws-history", role="user", content="what is 2+2",
        )
        self._pool.add_message(
            conversation_id="conv-ws-history", role="assistant", content="4",
        )

        upstream_payloads = []

        async def capturing_core(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            upstream_payloads.append(body)
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            await response.write(
                (json.dumps({
                    "type": "message_complete",
                    "message_id": "done",
                    "tokens_in": 4,
                    "tokens_out": 2,
                    "elapsed_ms": 10,
                }) + "\n").encode()
            )
            await response.write_eof()
            return response

        core_app = web.Application()
        core_app.router.add_post("/api/chat", capturing_core)
        core_server = TestServer(core_app)
        self._run(core_server.start_server())

        original_core_url = config.CORE_URL
        config.CORE_URL = str(core_server.make_url("/")).rstrip("/")

        try:
            token = self._token()
            ws = self._run(
                self._client.ws_connect(
                    "/ws/chat",
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
            self._run(
                ws.send_json({
                    "type": "message",
                    "conversation_id": "conv-ws-history",
                    "content": "what is 3+3",
                })
            )
            self._run(ws.receive_json())  # message_complete
            self._run(ws.close())
        finally:
            config.CORE_URL = original_core_url
            self._run(core_server.close())

        self.assertEqual(len(upstream_payloads), 1)
        history = upstream_payloads[0].get("history") or []
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["role"], "user")
        self.assertEqual(history[0]["content"], "what is 2+2")
        self.assertEqual(history[1]["role"], "assistant")
        self.assertEqual(history[1]["content"], "4")

    def test_second_message_supersedes_first_stream(self):
        """When a second message arrives for the same user, the first stream
        is cancelled and the displaced client receives generation_stopped
        (code=superseded) instead of an empty message_complete."""
        from aiohttp import web
        from config import config

        stream_requests = []

        async def slow_core(request: web.Request) -> web.StreamResponse:
            body = await request.json()
            stream_requests.append(body.get("content"))
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            await response.prepare(request)
            for i in range(10):
                await response.write(
                    (json.dumps({"type": "chunk", "content": f"word{i} "}) + "\n").encode()
                )
                await asyncio.sleep(0.05)
            await response.write(
                (json.dumps({
                    "type": "message_complete",
                    "message_id": "done",
                    "tokens_in": 4,
                    "tokens_out": 2,
                    "elapsed_ms": 100,
                }) + "\n").encode()
            )
            await response.write_eof()
            return response

        slow_app = web.Application()
        slow_app.router.add_post("/api/chat", slow_core)
        slow_server = TestServer(slow_app)
        self._run(slow_server.start_server())

        original_core_url = config.CORE_URL
        config.CORE_URL = str(slow_server.make_url("/")).rstrip("/")

        try:
            token = self._token()

            # Client A — starts a slow stream
            ws_a = self._run(
                self._client.ws_connect(
                    "/ws/chat",
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
            self._run(
                ws_a.send_json({
                    "type": "message",
                    "conversation_id": "conv-ws",
                    "content": "First message",
                })
            )
            # Confirm stream started
            first_a = self._run(ws_a.receive_json())
            self.assertEqual(first_a["type"], "chunk")

            # Client B (same user) — sends second message, supersedes A's stream
            ws_b = self._run(
                self._client.ws_connect(
                    "/ws/chat",
                    headers={"Authorization": f"Bearer {token}"},
                )
            )
            self._run(
                ws_b.send_json({
                    "type": "message",
                    "conversation_id": "conv-ws",
                    "content": "Second message",
                })
            )

            # Client A must receive generation_stopped (superseded)
            msg_a = self._run(ws_a.receive_json())
            self.assertEqual(msg_a["type"], "generation_stopped")
            self.assertEqual(msg_a["code"], "superseded")

            # Client B gets normal stream
            first_b = self._run(ws_b.receive_json())
            self.assertEqual(first_b["type"], "chunk")

            self._run(ws_a.close())
            self._run(ws_b.close())
        finally:
            config.CORE_URL = original_core_url
            self._run(slow_server.close())

    def test_switch_model_scoped_to_conversation(self):
        """Pin model/backend in conv-ws; conv-other must remain unpinned."""
        self.core_requests.clear()

        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )

        # Pin in conv-ws
        self._run(ws.send_json({
            "type": "switch_model", "model": "gpt-5", "backend": "openai",
            "conversation_id": "conv-ws",
        }))
        resp = self._run(ws.receive_json())
        self.assertEqual(resp["type"], "model_switched")

        # Message in conv-ws — should carry pinned model/backend upstream
        self._run(ws.send_json({
            "type": "message", "conversation_id": "conv-ws", "content": "Hi",
        }))
        self._run(ws.receive_json())  # chunk
        self._run(ws.receive_json())  # chunk
        self._run(ws.receive_json())  # message_complete

        # Message in conv-other — must NOT carry the pin
        self._run(ws.send_json({
            "type": "message", "conversation_id": "conv-other", "content": "Hi other",
        }))
        self._run(ws.receive_json())  # chunk
        self._run(ws.receive_json())  # chunk
        self._run(ws.receive_json())  # message_complete

        self.assertEqual(len(self.core_requests), 2)
        # conv-ws request has the pin
        self.assertEqual(self.core_requests[0].get("model"), "gpt-5")
        self.assertEqual(self.core_requests[0].get("backend"), "openai")
        # conv-other request is unpinned
        self.assertIsNone(self.core_requests[1].get("model"))
        self.assertIsNone(self.core_requests[1].get("backend"))

        self._run(ws.close())

    def test_unpinned_message_sends_no_backend(self):
        """A message without prior switch_model must send backend=None upstream."""
        self.core_requests.clear()

        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )

        self._run(ws.send_json({
            "type": "message", "conversation_id": "conv-ws", "content": "Unpinned",
        }))
        self._run(ws.receive_json())  # chunk
        self._run(ws.receive_json())  # chunk
        self._run(ws.receive_json())  # message_complete

        self.assertEqual(len(self.core_requests), 1)
        self.assertIsNone(self.core_requests[0].get("model"))
        self.assertIsNone(self.core_requests[0].get("backend"))

        self._run(ws.close())

    def test_unpinned_chunk_carries_resolved_backend(self):
        """E-OBS relay: even when the client sends no pin (AUTO), the gateway
        must forward the backend core resolved on the chunk — never blank. The
        mock core echoes backend='core' for an unpinned turn; the gateway relays
        it so the UI can show who answered."""
        self.core_requests.clear()

        ws = self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )
        self._run(ws.send_json({
            "type": "message", "conversation_id": "conv-ws", "content": "Unpinned",
        }))
        first = self._run(ws.receive_json())
        self.assertEqual(first["type"], "chunk")
        # Upstream payload was unpinned (backend=None) ...
        self.assertIsNone(self.core_requests[0].get("backend"))
        # ... but the chunk relayed to the client carries core's resolved backend.
        self.assertEqual(first["backend"], "core")

        self._run(ws.close())
