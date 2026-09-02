import asyncio
import json
import unittest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from app_state import CONVERSATION_STATE_KEY, POSTGRES_POOL_KEY, SESSION_STATE_KEY
from auth import create_access_token
from config import config
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool

CHUNK_COUNT = 10
FULL_ASSISTANT_TEXT = "".join(f"word{i} " for i in range(CHUNK_COUNT))


class TestChatConcurrency(unittest.TestCase):
    """Per-conversation turn lifecycle + per-user concurrency cap (Wave 1A Task 1).

    Hermetic: a slow in-process aiohttp "core" streams canned chunks, the DB is
    the InMemoryPostgresPool — no network, no postgres.
    """

    _loop: asyncio.AbstractEventLoop
    _client: TestClient
    _core_server: TestServer
    _pool: InMemoryPostgresPool
    _original_core_url: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            async def slow_core(request: web.Request) -> web.StreamResponse:
                response = web.StreamResponse(headers={"Content-Type": "application/json"})
                await response.prepare(request)
                for i in range(CHUNK_COUNT):
                    await response.write(
                        (json.dumps({"type": "chunk", "content": f"word{i} "}) + "\n").encode()
                    )
                    await asyncio.sleep(0.02)
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

            core_app = web.Application()
            core_app.router.add_post("/api/chat", slow_core)
            cls._core_server = TestServer(core_app)
            await cls._core_server.start_server()

            cls._original_core_url = config.CORE_URL
            config.CORE_URL = str(cls._core_server.make_url("/")).rstrip("/")

            cls._pool = InMemoryPostgresPool()
            for n in range(1, 6):
                cls._pool.add_conversation(
                    title=f"Conversation {n}", conv_id=f"conv-{n}", user_id="bobclaw"
                )
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
        self._client.server.app[CONVERSATION_STATE_KEY].clear()
        self._client.server.app[SESSION_STATE_KEY].clear()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _token(self):
        return create_access_token("bobclaw")

    def _connect(self):
        return self._run(
            self._client.ws_connect(
                "/ws/chat",
                headers={"Authorization": f"Bearer {self._token()}"},
            )
        )

    def _send_message(self, ws, conversation_id: str, content: str = "Hi") -> None:
        self._run(ws.send_json({
            "type": "message",
            "conversation_id": conversation_id,
            "content": content,
        }))

    def _receive_until(self, ws, predicate, cap: int = 200) -> list[dict]:
        """Collect frames until predicate(frame) is true (inclusive)."""
        frames = []
        for _ in range(cap):
            frame = self._run(ws.receive_json())
            frames.append(frame)
            if predicate(frame):
                return frames
        self.fail(f"frame satisfying predicate not received within {cap} frames")

    def _assistant_rows(self, conversation_id: str) -> list[dict]:
        return [
            m for m in self._pool.messages
            if m["conversation_id"] == conversation_id and m["role"] == "assistant"
        ]

    def test_two_conversations_stream_concurrently(self):
        """Streams in two conversations of the same user run side by side —
        neither cancels the other (no generation_stopped), both persist."""
        ws = self._connect()
        self._send_message(ws, "conv-1", "First conversation")
        first = self._run(ws.receive_json())
        self.assertEqual(first["type"], "chunk")  # conv-1 stream is live

        self._send_message(ws, "conv-2", "Second conversation")
        completes = [first]
        self._receive_until(
            ws,
            lambda f: (completes.append(f), sum(1 for x in completes if x["type"] == "message_complete") == 2)[1],
        )
        self._run(ws.close())

        types = [f["type"] for f in completes]
        self.assertEqual(types.count("message_complete"), 2)
        self.assertEqual(types.count("chunk"), 2 * CHUNK_COUNT)
        self.assertNotIn("generation_stopped", types)
        # Both turns persisted their full assistant message.
        self.assertEqual([r["content"] for r in self._assistant_rows("conv-1")], [FULL_ASSISTANT_TEXT])
        self.assertEqual([r["content"] for r in self._assistant_rows("conv-2")], [FULL_ASSISTANT_TEXT])

    def test_cleanup_race_keeps_replacement_stream(self):
        """Task A cancelled+replaced by task B in the same conversation: A's late
        done-callback must not erase B from conv_session['active_stream'], and no
        CancelledError escapes the callback (task.exception() guarded)."""
        loop_errors: list = []
        previous_handler = self._loop.get_exception_handler()
        self._loop.set_exception_handler(lambda loop, ctx: loop_errors.append(ctx))
        try:
            ws = self._connect()
            self._send_message(ws, "conv-1", "First")
            first = self._run(ws.receive_json())
            self.assertEqual(first["type"], "chunk")

            # Second message supersedes A with B in the same conversation.
            self._send_message(ws, "conv-1", "Second")
            stopped = self._run(ws.receive_json())
            self.assertEqual(stopped["type"], "generation_stopped")
            self.assertEqual(stopped["code"], "superseded")

            # Let A's cancelled task finish and its done-callback run.
            self._run(asyncio.sleep(0.05))
            conv_session = self._client.server.app[CONVERSATION_STATE_KEY]["bobclaw:conv-1"]
            replacement = conv_session.get("active_stream")
            self.assertIsNotNone(replacement)  # A's callback did NOT erase B
            self.assertFalse(replacement.done())

            # B completes; its own callback then clears the slot.
            self._receive_until(ws, lambda f: f["type"] == "message_complete")
            self._run(asyncio.sleep(0.05))
            self.assertIsNone(conv_session.get("active_stream"))
            self._run(ws.close())
        finally:
            self._loop.set_exception_handler(previous_handler)
        self.assertEqual(loop_errors, [])  # no CancelledError / callback exceptions

    def test_fifth_concurrent_stream_rejected_with_concurrency_limit(self):
        """Cap=4 (overridden via config): a 5th conversation's stream is rejected
        with a WS error code concurrency_limit instead of starting."""
        original_cap = config.MAX_CONCURRENT_STREAMS_PER_USER
        config.MAX_CONCURRENT_STREAMS_PER_USER = 4
        try:
            ws = self._connect()
            for n in range(1, 5):
                self._send_message(ws, f"conv-{n}", f"Stream {n}")
            # Confirm all four streams are live (one chunk each).
            for _ in range(4):
                frame = self._run(ws.receive_json())
                self.assertEqual(frame["type"], "chunk")

            self._send_message(ws, "conv-5", "One too many")
            frames = self._receive_until(ws, lambda f: f["type"] == "error")
            self.assertEqual(frames[-1]["code"], "concurrency_limit")
            # Rejected before any side effect: conv-5's user message never saved.
            self.assertEqual(
                [m for m in self._pool.messages if m["conversation_id"] == "conv-5"], []
            )

            # Drain the four live streams cleanly.
            completes = []
            self._receive_until(
                ws,
                lambda f: (completes.append(f), sum(1 for x in completes if x["type"] == "message_complete") == 4)[1],
            )
            self._run(ws.close())
        finally:
            config.MAX_CONCURRENT_STREAMS_PER_USER = original_cap

    def test_superseded_turn_not_persisted(self):
        """A superseded turn's truncated assistant message is NOT saved — only the
        replacement turn's full message lands in the pool."""
        ws = self._connect()
        self._send_message(ws, "conv-1", "First")
        first = self._run(ws.receive_json())
        self.assertEqual(first["type"], "chunk")

        self._send_message(ws, "conv-1", "Second")
        stopped = self._run(ws.receive_json())
        self.assertEqual(stopped["type"], "generation_stopped")
        self.assertEqual(stopped["code"], "superseded")
        self._receive_until(ws, lambda f: f["type"] == "message_complete")
        self._run(asyncio.sleep(0.05))  # let the superseded task's tail finish
        self._run(ws.close())

        rows = self._assistant_rows("conv-1")
        self.assertEqual(len(rows), 1)  # no truncated row from the superseded turn
        self.assertEqual(rows[0]["content"], FULL_ASSISTANT_TEXT)

    def test_manual_stop_persists_partial_message(self):
        """Manual stop_generation keeps the two-stage stop semantics: the stream
        is cancelled, generation_stopped + message_complete are sent, AND the
        truncated assistant message IS persisted."""
        ws = self._connect()
        self._send_message(ws, "conv-1", "Keep talking")
        first = self._run(ws.receive_json())
        self.assertEqual(first["type"], "chunk")

        self._run(ws.send_json({"type": "stop_generation", "conversation_id": "conv-1"}))
        stopped = self._run(ws.receive_json())
        self.assertEqual(stopped["type"], "generation_stopped")
        self.assertEqual(stopped["code"], "stopped")
        complete = self._run(ws.receive_json())
        self.assertEqual(complete["type"], "message_complete")
        self._run(ws.close())

        rows = self._assistant_rows("conv-1")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["content"].startswith("word0 "))
        self.assertNotEqual(rows[0]["content"], FULL_ASSISTANT_TEXT)  # truncated
    def test_cap_reservation_atomic_under_concurrent_connects(self):
        """Audit 1A (Opus) finding: the cap check and the slot reservation must be
        atomic. With cap=1 and connection A parked mid-turn-setup (history fetch
        blocked), connection B's message must be rejected — the pre-fix code only
        reserved the slot AFTER the awaits, so B would pass the same check."""
        from routers import chat as chat_module

        original_cap = config.MAX_CONCURRENT_STREAMS_PER_USER
        config.MAX_CONCURRENT_STREAMS_PER_USER = 1
        original_history = chat_module._get_conversation_history
        gate = asyncio.Event()

        async def slow_history(pool, conversation_id, *, limit, max_chars):
            await gate.wait()
            return []

        chat_module._get_conversation_history = slow_history
        try:
            ws_a = self._connect()
            self._send_message(ws_a, "conv-1", "A")
            self._run(asyncio.sleep(0.1))  # let A reach the blocked history fetch

            ws_b = self._connect()
            self._send_message(ws_b, "conv-2", "B")
            frame = self._run(ws_b.receive_json())
            self.assertEqual(frame["type"], "error")
            self.assertEqual(frame["code"], "concurrency_limit")
            # Rejection is side-effect-free: no conv-2 user message persisted.
            self.assertEqual(
                [m for m in self._pool.messages if m["conversation_id"] == "conv-2"], []
            )

            gate.set()  # release A — it completes normally
            self._receive_until(ws_a, lambda f: f["type"] == "message_complete")
            self._run(ws_a.close())
            self._run(ws_b.close())
        finally:
            chat_module._get_conversation_history = original_history
            config.MAX_CONCURRENT_STREAMS_PER_USER = original_cap


if __name__ == "__main__":
    unittest.main()
