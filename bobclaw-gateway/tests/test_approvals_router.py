import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool
from tests.fake_redis import FakeRedis
import redis_client


class _FakeResponse:
    def __init__(self, status: int = 200, body: str = "ok") -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakeContextManager:
    def __init__(self, value) -> None:
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeClientSession:
    """Stub aiohttp.ClientSession for mocking the core proxy in decide tests."""

    response_status: int = 200
    response_body: str = "ok"

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def post(self, url, **kwargs):
        return _FakeContextManager(_FakeResponse(self.response_status, self.response_body))


class TestApprovalsRoutes(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient
    _pool: InMemoryPostgresPool
    _redis: FakeRedis

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            cls._pool = InMemoryPostgresPool()
            cls._redis = FakeRedis()
            redis_client.set_redis_client(cls._redis)
            cls._client = TestClient(
                TestServer(build_app({POSTGRES_POOL_KEY: cls._pool}))
            )
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        redis_client.set_redis_client(None)
        cls._loop.close()

    def setUp(self) -> None:
        self._pool.approvals.clear()
        self._pool._approval_seq = 0
        self._redis.published.clear()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth_headers(self, user: str = "bobclaw"):
        token = create_access_token(user)
        return {"Authorization": f"Bearer {token}"}

    def _uuid(self) -> str:
        return str(uuid4())

    def test_ws_approvals_is_auth_exempt(self):
        """Browsers can't set an Authorization header on a WS upgrade, so
        /ws/approvals must be exempt from auth_middleware (it runs its own
        first-frame auth). A middleware 401 would raise on connect; reaching a
        live socket proves the exemption."""
        ws = self._run(self._client.ws_connect("/ws/approvals"))
        self.assertFalse(ws.closed)  # upgrade succeeded, not pre-401'd
        self._run(ws.close())

    def test_list_pending_default(self):
        a1 = self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw", status="pending")
        self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw", status="approved")
        resp = self._run(self._client.get("/approvals", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [a1["id"]])
        self.assertEqual(data["status"], "pending")

    def test_list_filter_by_status(self):
        self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw", status="pending")
        approved = self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw", status="approved")
        resp = self._run(
            self._client.get("/approvals?status=approved", headers=self._auth_headers())
        )
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [approved["id"]])

    def test_list_filter_all_returns_every_status(self):
        self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw", status="pending")
        self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw", status="approved")
        self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw", status="rejected")
        resp = self._run(
            self._client.get("/approvals?status=all", headers=self._auth_headers())
        )
        data = self._run(resp.json())
        self.assertEqual(len(data["items"]), 3)

    def test_list_invalid_status_rejected(self):
        resp = self._run(
            self._client.get("/approvals?status=bogus", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 400)

    def test_get_approval(self):
        a = self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            details={"face_id": "researcher", "task": "send email"},
        )
        resp = self._run(
            self._client.get(f"/approvals/{a['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["id"], a["id"])
        self.assertEqual(data["details"]["face_id"], "researcher")

    def test_get_missing_approval_404(self):
        resp = self._run(
            self._client.get(f"/approvals/{self._uuid()}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

    def test_decide_approve_marks_row(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw")

        with patch("routers.approvals.aiohttp.ClientSession", _FakeClientSession):
            resp = self._run(
                self._client.post(
                    f"/approvals/{a['id']}/decide",
                    json={"decision": "approve"},
                    headers=self._auth_headers(),
                )
            )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["agent_resume"], "ok")
        self.assertEqual(self._pool.approvals[a["id"]]["status"], "approved")
        self.assertIsNotNone(self._pool.approvals[a["id"]]["decided_at"])

    def test_decide_reject_marks_row(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw")

        with patch("routers.approvals.aiohttp.ClientSession", _FakeClientSession):
            resp = self._run(
                self._client.post(
                    f"/approvals/{a['id']}/decide",
                    json={"decision": "reject"},
                    headers=self._auth_headers(),
                )
            )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["agent_resume"], "ok")
        self.assertEqual(self._pool.approvals[a["id"]]["status"], "rejected")

    def test_decide_invalid_decision_rejected(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw")
        resp = self._run(
            self._client.post(
                f"/approvals/{a['id']}/decide",
                json={"decision": "maybe"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_decide_already_decided_returns_404(self):
        a = self._pool.add_approval(
            approval_id=self._uuid(), user_id="bobclaw", status="approved"
        )
        resp = self._run(
            self._client.post(
                f"/approvals/{a['id']}/decide",
                json={"decision": "reject"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 404)

    def test_decide_with_core_failure_still_records_decision(self):
        """Even if core proxy fails, the local row is updated and we surface
        agent_resume=failed so the dashboard reflects the user's intent."""
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw")

        def boom(*args, **kwargs):
            raise RuntimeError("core unreachable")

        with patch("routers.approvals.aiohttp.ClientSession", boom):
            resp = self._run(
                self._client.post(
                    f"/approvals/{a['id']}/decide",
                    json={"decision": "approve"},
                    headers=self._auth_headers(),
                )
            )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["agent_resume"], "failed")
        self.assertEqual(data["status"], "approved")
        self.assertEqual(self._pool.approvals[a["id"]]["status"], "approved")

    def test_cc_edit_lists_and_decides_with_jwt(self):
        """A cc_edit approval (C4) lists in /approvals and decides via the
        existing proxy. JWT is required — the gateway is action_type-agnostic,
        so no new auth exemption is added."""
        a = self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            action_type="cc_edit",
            details={
                "file_path": "hello.txt",
                "diff": "--- a/hello.txt\n+++ b/hello.txt\n@@ -1 +1 @@\n-hello\n+hi\n",
                "scope": {"branch": "feat/x", "may_touch": ["hello.txt"]},
            },
        )

        # Lists with action_type cc_edit + details preserved.
        resp = self._run(self._client.get("/approvals", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        ids = [item["id"] for item in data["items"]]
        self.assertIn(a["id"], ids)
        item = next(i for i in data["items"] if i["id"] == a["id"])
        self.assertEqual(item["action_type"], "cc_edit")
        self.assertEqual(item["details"]["file_path"], "hello.txt")

        # Decides via the existing proxy (mocked core).
        with patch("routers.approvals.aiohttp.ClientSession", _FakeClientSession):
            resp = self._run(
                self._client.post(
                    f"/approvals/{a['id']}/decide",
                    json={"decision": "approve", "edit_content": "edited diff"},
                    headers=self._auth_headers(),
                )
            )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["status"], "approved")
        self.assertEqual(data["agent_resume"], "ok")

    def test_cc_edit_decide_requires_jwt(self):
        """No Authorization header → the decide endpoint is not reachable."""
        a = self._pool.add_approval(
            approval_id=self._uuid(), user_id="bobclaw", action_type="cc_edit"
        )
        resp = self._run(
            self._client.post(
                f"/approvals/{a['id']}/decide", json={"decision": "approve"}
            )
        )
        self.assertEqual(resp.status, 401)

    def test_approved_by_present_in_list(self):
        """A gate-cleared row surfaces its approved_by in the list response."""
        self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            status="approved",
            approved_by="gate",
            action_type="worker_scope_review",
        )
        resp = self._run(
            self._client.get("/approvals?status=all", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["approved_by"], "gate")

    def test_approved_by_present_in_detail(self):
        """A human-required row exposes approved_by = None in the detail view."""
        a = self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            status="pending",
            approved_by=None,
            action_type="worker_scope_review",
        )
        resp = self._run(
            self._client.get(f"/approvals/{a['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertIn("approved_by", data)
        self.assertIsNone(data["approved_by"])

    def test_digest_returns_gate_cleared_and_flagged_pending(self):
        """The digest splits gate-cleared (approved_by='gate') from
        flagged-pending (pending worker_scope_review) with counts."""
        # Gate auto-cleared (audit, non-blocking)
        self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            status="approved",
            approved_by="gate",
            action_type="worker_scope_review",
        )
        self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            status="approved",
            approved_by="gate",
            action_type="worker_scope_review",
        )
        # Flagged → needs human review
        self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            status="pending",
            approved_by=None,
            action_type="worker_scope_review",
        )
        # Noise that must NOT appear in either slice: a pending non-gate-review
        # approval (e.g. a task_approval) and another user's gate row.
        self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            status="pending",
            action_type="task_approval",
        )
        self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="intruder",
            status="approved",
            approved_by="gate",
            action_type="worker_scope_review",
        )

        resp = self._run(
            self._client.get("/approvals/digest", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["counts"]["gate_cleared"], 2)
        self.assertEqual(data["counts"]["flagged_pending"], 1)
        self.assertEqual(len(data["gate_cleared"]), 2)
        self.assertEqual(len(data["flagged_pending"]), 1)
        for item in data["gate_cleared"]:
            self.assertEqual(item["approved_by"], "gate")
        for item in data["flagged_pending"]:
            self.assertEqual(item["status"], "pending")
            self.assertEqual(item["action_type"], "worker_scope_review")

    def test_digest_empty_when_no_gate_activity(self):
        """No gate rows → both slices empty, counts zero."""
        self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="bobclaw",
            status="pending",
            action_type="task_approval",
        )
        resp = self._run(
            self._client.get("/approvals/digest", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["counts"], {"gate_cleared": 0, "flagged_pending": 0})
        self.assertEqual(data["gate_cleared"], [])
        self.assertEqual(data["flagged_pending"], [])

    def test_cross_user_approval_isolation(self):
        intruder = self._pool.add_approval(approval_id=self._uuid(), user_id="intruder")
        mine = self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw")

        # List shows only mine
        resp = self._run(self._client.get("/approvals", headers=self._auth_headers()))
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [mine["id"]])

        # GET other user's → 404
        resp = self._run(
            self._client.get(f"/approvals/{intruder['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

        # POST decide other user's → 404
        resp = self._run(
            self._client.post(
                f"/approvals/{intruder['id']}/decide",
                json={"decision": "approve"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 404)


class TestChatPersistence(unittest.TestCase):
    """Verify chat.py persists approval rows + publishes to Redis when core
    emits an approval_request event in the SSE forward path."""

    def setUp(self) -> None:
        self.pool = InMemoryPostgresPool()
        self.redis = FakeRedis()
        redis_client.set_redis_client(self.redis)

    def tearDown(self) -> None:
        redis_client.set_redis_client(None)

    def test_persist_writes_row(self):
        from routers.chat import _persist_approval

        loop = asyncio.new_event_loop()
        try:
            approval_uuid = str(uuid4())
            conv_uuid = str(uuid4())
            loop.run_until_complete(_persist_approval(
                self.pool,
                approval_id_hex=approval_uuid,
                conversation_id=conv_uuid,
                user_id="bobclaw",
                action_type="task_approval",
                details={"face_id": "researcher", "task": "send email"},
            ))
        finally:
            loop.close()

        self.assertIn(approval_uuid, self.pool.approvals)
        row = self.pool.approvals[approval_uuid]
        self.assertEqual(row["user_id"], "bobclaw")
        self.assertEqual(row["action_type"], "task_approval")
        self.assertEqual(row["details"]["face_id"], "researcher")
        self.assertEqual(row["status"], "pending")

    def test_persist_bad_uuid_silently_skips(self):
        from routers.chat import _persist_approval

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_persist_approval(
                self.pool,
                approval_id_hex="not-a-uuid",
                conversation_id=str(uuid4()),
                user_id="bobclaw",
                action_type="task_approval",
                details={},
            ))
        finally:
            loop.close()
        self.assertEqual(len(self.pool.approvals), 0)

    def test_persist_no_pool_silent(self):
        from routers.chat import _persist_approval

        loop = asyncio.new_event_loop()
        try:
            # Should not raise
            loop.run_until_complete(_persist_approval(
                None,
                approval_id_hex=str(uuid4()),
                conversation_id=str(uuid4()),
                user_id="bobclaw",
                action_type="task_approval",
                details={},
            ))
        finally:
            loop.close()

    def test_publish_writes_to_redis(self):
        from routers.chat import _publish_approval

        payload = {"type": "new_approval", "approval_id": "abc"}
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_publish_approval("bobclaw", payload))
        finally:
            loop.close()

        self.assertEqual(len(self.redis.published), 1)
        channel, message = self.redis.published[0]
        self.assertEqual(channel, "bobclaw:approvals:bobclaw")
        self.assertEqual(json.loads(message), payload)

    def test_publish_redis_failure_silent(self):
        from routers.chat import _publish_approval

        class BoomRedis:
            async def publish(self, *args, **kwargs):
                raise RuntimeError("redis down")

        redis_client.set_redis_client(BoomRedis())
        loop = asyncio.new_event_loop()
        try:
            # Should not raise
            loop.run_until_complete(_publish_approval("bobclaw", {"x": 1}))
        finally:
            loop.close()
