"""Resume-output delivery on the REST decide path.

Defect (verified live 2026-10-24, receipts/approval-allow-effect-2026-10-24.json):
``POST /approvals/{id}/decide`` recorded the decision and proxied core's
``/api/chat/approval`` resume, but never read the resulting SSE stream. The approved
turn therefore ran and was billed (two provider completions observed) while the
conversation still held only the user row — the decision was visible, the effect was
not.

The fix under test:
  * the decide reply is sent as soon as core accepts the resume (headers), and the
    response stream is drained by a background task — a long approved turn cannot
    delay or outlive the decision reply;
  * ``_persist_resume_output`` parses SSE at LINE boundaries (frames straddle network
    reads) and writes exactly ONE assistant row on ``message_complete``;
  * a second gated step inside the resume persists its own approval row and is
    published, instead of being dropped with the stream;
  * a rejection (system message, no assistant output) is reported as
    ``no_output_expected`` rather than as a lost answer;
  * nothing is persisted without a completion, and no failure of the stream read ever
    turns the recorded decision into a failure.

These are UNIT tests against the router module and an in-memory pool. They do not
replace live acceptance: only a real approval turn through core proves delivery.
"""
import asyncio
import json
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token
from gateway import build_app
from routers.approvals import _persist_resume_output
from tests.fake_pool import InMemoryPostgresPool
from tests.fake_redis import FakeRedis
import redis_client


class _StreamingResponse:
    """Minimal stand-in for an aiohttp response with an SSE body."""

    def __init__(self, lines, status: int = 200, gate: "asyncio.Event | None" = None) -> None:
        self.status = status
        self._lines = list(lines)
        self._gate = gate
        self.content = self._iter()

    async def _iter(self):
        for index, line in enumerate(self._lines):
            yield line
            if self._gate is not None and index == 0:
                # Hold the rest of the stream until the test releases it: proves the
                # decide reply does not wait for the whole resumed turn.
                await asyncio.wait_for(self._gate.wait(), timeout=5)

    async def text(self) -> str:
        return ""


def _sse(*events: dict) -> bytes:
    return "".join("data: " + json.dumps(e) + "\n" for e in events).encode()


def _chunk(text: str) -> dict:
    return {"type": "chunk", "content": text}


def _complete(**kwargs) -> dict:
    base = {"type": "message_complete", "message_id": "m-1"}
    base.update(kwargs)
    return base


def _metadata(row) -> dict:
    meta = row["metadata"]
    return json.loads(meta) if isinstance(meta, str) else meta


class TestPersistResumeOutput(unittest.TestCase):
    def setUp(self) -> None:
        self.pool = InMemoryPostgresPool()
        self.conv = str(uuid4())
        # Keep the nested-approval publish offline: the helper must not reach a broker.
        redis_client.set_redis_client(FakeRedis())

    def tearDown(self) -> None:
        redis_client.set_redis_client(None)

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def _drain(self, response, conversation_id="__conv__", decision="approve"):
        return self._run(
            _persist_resume_output(
                response, self.pool,
                conversation_id=self.conv if conversation_id == "__conv__" else conversation_id,
                user_id="admin", approval_id_hex="abc123", decision=decision,
            )
        )

    def test_persists_one_assistant_row_on_completion(self):
        response = _StreamingResponse([
            _sse(_chunk("PORTAL"), _chunk("_APPROVED_OK")),
            _sse(_complete(tokens_in=7, tokens_out=3)),
        ])
        result = self._drain(response)
        self.assertEqual(result["status"], "persisted")
        self.assertEqual(len(self.pool.messages), 1)
        row = self.pool.messages[0]
        self.assertEqual(row["role"], "assistant")
        self.assertEqual(row["content"], "PORTAL_APPROVED_OK")
        self.assertEqual(row["conversation_id"], self.conv)
        meta = _metadata(row)
        self.assertEqual(meta["resumed_from_approval"], "abc123")
        self.assertEqual(meta["tokens_in"], 7)

    def test_frame_split_across_reads_is_reassembled(self):
        """A `data:` frame split over two socket reads must not be stored as raw JSON."""
        frame = _sse(_chunk("SPLIT_OK"), _complete())
        response = _StreamingResponse([frame[:9], frame[9:]])
        result = self._drain(response)
        self.assertEqual(result["status"], "persisted")
        self.assertEqual(self.pool.messages[0]["content"], "SPLIT_OK")

    def test_completion_split_across_reads_is_not_missed(self):
        tail = _sse(_complete())
        response = _StreamingResponse([_sse(_chunk("X")), tail[:6], tail[6:]])
        result = self._drain(response)
        self.assertEqual(result["status"], "persisted")

    def test_chunks_without_completion_do_not_persist(self):
        result = self._drain(_StreamingResponse([_sse(_chunk("half an ans"))]))
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(self.pool.messages, [])

    def test_error_event_without_chunks_is_error(self):
        result = self._drain(_StreamingResponse([_sse({"type": "error", "message": "backend exploded"})]))
        self.assertEqual(result["status"], "error")
        self.assertIn("backend exploded", result["detail"])
        self.assertEqual(self.pool.messages, [])

    def test_reject_with_no_assistant_output_is_not_a_lost_answer(self):
        # Real reject shape: zero chunks, but core still emits message_complete.
        result = self._drain(
            _StreamingResponse([_sse(_complete(tokens_in=0, tokens_out=0))]), decision="reject"
        )
        self.assertEqual(result["status"], "no_output_expected")
        self.assertEqual(self.pool.messages, [])

    def test_non_data_lines_are_not_stored_as_answers(self):
        response = _StreamingResponse([
            b"event: message_complete\n",
            b": keep-alive\n",
            _sse(_chunk("REAL"), _complete()),
        ])
        result = self._drain(response)
        self.assertEqual(result["status"], "persisted")
        self.assertEqual(self.pool.messages[0]["content"], "REAL")

    def test_second_gated_step_persists_its_own_approval(self):
        nested = uuid4().hex
        response = _StreamingResponse([
            _sse(_chunk("about to act")),
            _sse({"type": "approval_request", "approval_id": nested,
                  "action": "task_approval", "details": {"task": "second step"}}),
        ])
        # A real asyncpg row hands back a uuid.UUID, not a string (review round 2, N1).
        result = self._drain(response, conversation_id=UUID(self.conv))
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertEqual(result["approval_id"], nested)
        stored = str(UUID(nested))
        self.assertIn(stored, self.pool.approvals, "nested approval row was not written")
        self.assertEqual(self.pool.approvals[stored]["status"], "pending")
        # The pre-gate text is kept (parity with the WS chat path), marked partial.
        self.assertEqual(len(self.pool.messages), 1)
        self.assertEqual(self.pool.messages[0]["content"], "about to act")
        self.assertTrue(_metadata(self.pool.messages[0])["partial"])

    def test_row_without_conversation_is_drained_and_discarded(self):
        response = _StreamingResponse([_sse(_chunk("ANSWER"), _complete())])
        result = self._drain(response, conversation_id=None)
        self.assertEqual(result["status"], "discarded_no_conversation")
        self.assertEqual(self.pool.messages, [])


def _streaming_session(lines, gate=None):
    class _Session:
        def __init__(self, *args, **kwargs) -> None:
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

        async def post(self, url, **kwargs):
            return _StreamingResponse(lines, gate=gate)

    return _Session


class TestDecideSchedulesDelivery(unittest.TestCase):
    """The decide reply is fast; the stream is drained afterwards."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            cls._pool = InMemoryPostgresPool()
            cls._redis = FakeRedis()
            redis_client.set_redis_client(cls._redis)
            cls._client = TestClient(TestServer(build_app({POSTGRES_POOL_KEY: cls._pool})))
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        redis_client.set_redis_client(None)
        cls._loop.close()

    def setUp(self) -> None:
        self._pool.approvals.clear()
        self._pool.messages.clear()

    def _post_decide(self, approval_id):
        return self._client.post(
            f"/approvals/{approval_id}/decide",
            json={"decision": "approve"},
            headers={"Authorization": f"Bearer {create_access_token('bobclaw')}"},
        )

    def test_decide_returns_before_the_resumed_turn_finishes(self):
        conv = str(uuid4())
        a = self._pool.add_approval(approval_id=str(uuid4()), user_id="bobclaw",
                                    conversation_id=conv)
        gate = asyncio.Event()
        lines = [_sse(_chunk("APPROVED")), _sse(_chunk("_RUN_OK"), _complete(tokens_in=1, tokens_out=2))]

        async def scenario():
            with patch("routers.approvals.aiohttp.ClientSession",
                       _streaming_session(lines, gate=gate)):
                resp = await self._post_decide(a["id"])
                body = await resp.json()
                # Reply is out while the stream is still parked on the gate.
                self.assertEqual(resp.status, 200)
                self.assertEqual(body["decision"], "recorded")
                self.assertEqual(body["agent_resume"], "ok")
                self.assertEqual(body["resume_delivery"], "scheduled")
                self.assertEqual(self._pool.messages, [])
                gate.set()
                for _ in range(50):
                    if self._pool.messages:
                        break
                    await asyncio.sleep(0.02)
            return body

        self._loop.run_until_complete(scenario())
        self.assertEqual(len(self._pool.messages), 1)
        self.assertEqual(self._pool.messages[0]["content"], "APPROVED_RUN_OK")
        self.assertEqual(self._pool.messages[0]["conversation_id"], conv)
        self.assertEqual(self._pool.approvals[a["id"]]["status"], "approved")


if __name__ == "__main__":
    unittest.main()
