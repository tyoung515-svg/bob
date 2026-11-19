"""Portal agent approval guard — decide-path tests.

Defect being fixed (DISCOVERY-API.md "Approval and live-progress hazards" #1):
with ``BOB_AGENT_INNER_GATES=open`` the REST middleware's agent default-deny is
skipped, so an agent bearer could reach ``POST /approvals/{id}/decide`` with no
non-agent identity check and mutate the approval row — while ``/ws/approvals``
rejects agent tokens unconditionally.

The guard under test (routers/approvals.py):
  * human tokens: behaviour identical to before (no env flag, no extra read);
  * agent tokens: require ``BOB_PORTAL_APPROVALS`` to be truthy, the row to be
    owned by the token's ``sub``, and the row to still be ``pending``;
  * every refusal is a typed 403 (``agent_approval_disabled`` /
    ``agent_approval_foreign`` / ``agent_approval_stale``) raised BEFORE any DB
    status write, so the row is bit-for-bit unchanged;
  * a recorded agent decision writes ``approved_by='agent:<sub>'`` and only then
    attempts the core resume.
"""
import asyncio
import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token, create_agent_token
from config import config
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


class _Ctx:
    def __init__(self, value) -> None:
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc) -> None:
        return None


def _recording_session(pool, approval_id, *, status: int = 200, body: str = "ok"):
    """Stub aiohttp.ClientSession that records the row state at POST time.

    Recording *inside* ``post`` is what proves ordering: the recorded snapshot
    shows whether the DB status transition happened before the core resume was
    attempted.
    """
    seen: list[dict] = []

    class _Session:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def close(self) -> None:
            return None

        async def post(self, url, **kwargs):
            row = pool.approvals.get(approval_id) or {}
            seen.append({
                "url": url,
                "json": kwargs.get("json"),
                "status": row.get("status"),
                "approved_by": row.get("approved_by"),
                "decided_at": row.get("decided_at"),
            })
            return _FakeResponse(status, body)

    return _Session, seen


def _failing_session(pool, approval_id, *, exc: Exception | None = None):
    """Stub session that records row state and then raises (core unreachable)."""
    seen: list[dict] = []
    error = exc or RuntimeError("core unreachable")

    class _Session:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc) -> None:
            return None

        async def close(self) -> None:
            return None

        async def post(self, url, **kwargs):
            row = pool.approvals.get(approval_id) or {}
            seen.append({
                "status": row.get("status"),
                "approved_by": row.get("approved_by"),
            })
            raise error

    return _Session, seen


def _exploding_session(*args, **kwargs):
    raise AssertionError("core resume must not be attempted on a refused decision")


class TestAgentApprovalGuard(unittest.TestCase):
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
        self.queries: list[str] = []
        self._orig_fetchrow = self._pool.fetchrow

        async def _spy(query, *args):
            self.queries.append(" ".join(query.lower().split()))
            return await self._orig_fetchrow(query, *args)

        self._pool.fetchrow = _spy

    def tearDown(self) -> None:
        self._pool.fetchrow = self._orig_fetchrow

    # -- helpers -----------------------------------------------------------
    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _uuid(self) -> str:
        return str(uuid4())

    def _human_headers(self, sub: str = "bobclaw") -> dict:
        return {"Authorization": f"Bearer {create_access_token(sub)}"}

    def _agent_headers(self, sub: str) -> dict:
        # Empty scope => no lane roots; AGENT_INNER_GATES=open is forced per test
        # so the middleware does not pre-deny the route (that is the defect's
        # precondition, not the guard).
        return {
            "Authorization": f"Bearer {create_agent_token(sub, scope={}, faces=[])}"
        }

    def _open_gates(self):
        return patch.object(config, "AGENT_INNER_GATES", "open")

    def _flag_on(self):
        return patch.dict(os.environ, {"BOB_PORTAL_APPROVALS": "1"})

    def _flag_off(self):
        env = {k: v for k, v in os.environ.items() if k != "BOB_PORTAL_APPROVALS"}
        return patch.dict(os.environ, env, clear=True)

    def _post(self, approval_id: str, headers: dict, decision: str = "approve"):
        return self._client.post(
            f"/approvals/{approval_id}/decide",
            json={"decision": decision},
            headers=headers,
        )

    def _assert_no_status_write(self) -> None:
        writes = [q for q in self.queries if q.startswith("update approvals")]
        self.assertEqual(writes, [], f"refused decision issued a DB write: {writes}")

    # -- human token: unchanged -------------------------------------------
    def test_human_token_decides_without_any_flag(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="bobclaw")
        session, seen = _recording_session(self._pool, a["id"])
        with self._flag_off(), patch(
            "routers.approvals.aiohttp.ClientSession", session
        ):
            resp = self._run(self._post(a["id"], self._human_headers()))
            self.assertEqual(resp.status, 200)
            data = self._run(resp.json())
        self.assertEqual(data["decision"], "recorded")
        self.assertEqual(data["agent_resume"], "ok")
        self.assertEqual(data["status"], "approved")
        # approved_by keeps the human sub — the exact pre-patch value.
        self.assertEqual(self._pool.approvals[a["id"]]["approved_by"], "bobclaw")
        # No guard read on the human path (only the UPDATE).
        self.assertEqual(len([q for q in self.queries if q.startswith("select")]), 0)
        self.assertEqual(len(seen), 1)

    def test_human_token_foreign_row_still_404(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="someone-else")
        before = dict(self._pool.approvals[a["id"]])
        with self._flag_on(), patch(
            "routers.approvals.aiohttp.ClientSession", _exploding_session
        ):
            resp = self._run(self._post(a["id"], self._human_headers("bobclaw")))
        self.assertEqual(resp.status, 404)
        self.assertEqual(self._pool.approvals[a["id"]], before)

    # -- agent token: flag off --------------------------------------------
    def test_agent_flag_off_is_disabled_and_row_untouched(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="dad")
        before = dict(self._pool.approvals[a["id"]])
        with self._open_gates(), self._flag_off(), patch(
            "routers.approvals.aiohttp.ClientSession", _exploding_session
        ):
            resp = self._run(self._post(a["id"], self._agent_headers("dad")))
            self.assertEqual(resp.status, 403)
            body = self._run(resp.json())
        self.assertEqual(body["detail"], "agent_approval_disabled")
        self.assertEqual(self._pool.approvals[a["id"]], before)
        self.assertIsNone(self._pool.approvals[a["id"]]["decided_at"])
        self._assert_no_status_write()

    # -- agent token: flag on, foreign ------------------------------------
    def test_agent_flag_on_foreign_row_refused(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="someone-else")
        before = dict(self._pool.approvals[a["id"]])
        with self._open_gates(), self._flag_on(), patch(
            "routers.approvals.aiohttp.ClientSession", _exploding_session
        ):
            resp = self._run(self._post(a["id"], self._agent_headers("dad")))
            self.assertEqual(resp.status, 403)
            body = self._run(resp.json())
        self.assertEqual(body["detail"], "agent_approval_foreign")
        self.assertEqual(self._pool.approvals[a["id"]], before)
        self._assert_no_status_write()

    # -- agent token: flag on, not pending --------------------------------
    def test_agent_flag_on_stale_row_refused(self):
        decided_at = datetime.now(timezone.utc) - timedelta(hours=1)
        a = self._pool.add_approval(
            approval_id=self._uuid(),
            user_id="dad",
            status="approved",
            approved_by="gate",
            decided_at=decided_at,
        )
        before = dict(self._pool.approvals[a["id"]])
        with self._open_gates(), self._flag_on(), patch(
            "routers.approvals.aiohttp.ClientSession", _exploding_session
        ):
            resp = self._run(self._post(a["id"], self._agent_headers("dad"), "reject"))
            self.assertEqual(resp.status, 403)
            body = self._run(resp.json())
        self.assertEqual(body["detail"], "agent_approval_stale")
        self.assertEqual(self._pool.approvals[a["id"]], before)
        self.assertEqual(self._pool.approvals[a["id"]]["approved_by"], "gate")
        self.assertEqual(self._pool.approvals[a["id"]]["decided_at"], decided_at)
        self._assert_no_status_write()

    def test_agent_flag_on_missing_row_404(self):
        missing = self._uuid()
        with self._open_gates(), self._flag_on(), patch(
            "routers.approvals.aiohttp.ClientSession", _exploding_session
        ):
            resp = self._run(self._post(missing, self._agent_headers("dad")))
        self.assertEqual(resp.status, 404)
        self._assert_no_status_write()

    # -- agent token: flag on, own pending row ----------------------------
    def test_agent_own_pending_row_decides_after_status_transition(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="dad")
        session, seen = _recording_session(self._pool, a["id"])
        with self._open_gates(), self._flag_on(), patch(
            "routers.approvals.aiohttp.ClientSession", session
        ):
            resp = self._run(self._post(a["id"], self._agent_headers("dad")))
            self.assertEqual(resp.status, 200)
            data = self._run(resp.json())

        self.assertEqual(data["decision"], "recorded")
        self.assertEqual(data["agent_resume"], "ok")
        self.assertEqual(data["status"], "approved")
        row = self._pool.approvals[a["id"]]
        self.assertEqual(row["status"], "approved")
        self.assertEqual(row["approved_by"], "agent:dad")
        self.assertIsNotNone(row["decided_at"])

        # Ordering proof: core was called once and saw the row already decided.
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["status"], "approved")
        self.assertEqual(seen[0]["approved_by"], "agent:dad")
        self.assertEqual(
            seen[0]["json"]["approval_id"], a["id"].replace("-", "")
        )

    def test_agent_own_pending_row_resume_failure_surfaces_failed(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="dad")
        session, seen = _failing_session(self._pool, a["id"])
        with self._open_gates(), self._flag_on(), patch(
            "routers.approvals.aiohttp.ClientSession", session
        ):
            resp = self._run(self._post(a["id"], self._agent_headers("dad"), "reject"))
            self.assertEqual(resp.status, 200)
            data = self._run(resp.json())

        self.assertEqual(data["decision"], "recorded")
        self.assertEqual(data["agent_resume"], "failed")
        self.assertIn("core unreachable", data["agent_resume_message"])
        row = self._pool.approvals[a["id"]]
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["approved_by"], "agent:dad")
        # The transition happened before the resume attempt even on failure.
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["status"], "rejected")
        self.assertEqual(seen[0]["approved_by"], "agent:dad")

    def test_agent_flag_rejects_non_truthy_values(self):
        a = self._pool.add_approval(approval_id=self._uuid(), user_id="dad")
        before = dict(self._pool.approvals[a["id"]])
        for value in ("0", "false", "", "no"):
            with self._open_gates(), patch.dict(
                os.environ, {"BOB_PORTAL_APPROVALS": value}
            ), patch("routers.approvals.aiohttp.ClientSession", _exploding_session):
                resp = self._run(self._post(a["id"], self._agent_headers("dad")))
                self.assertEqual(resp.status, 403, f"flag={value!r} should be off")
                body = self._run(resp.json())
            self.assertEqual(body["detail"], "agent_approval_disabled")
        self.assertEqual(self._pool.approvals[a["id"]], before)


if __name__ == "__main__":
    unittest.main()
