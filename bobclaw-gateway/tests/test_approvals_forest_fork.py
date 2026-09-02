"""MS9-F7 — gateway approvals surface: the additive ``forest_fork`` approval kind.

Pins the F7 gateway accept criterion: the approvals surface serves the new ``forest_fork`` kind
additively (kinds registry + a ``forest_fork`` proposal flows through list/get/decide), and it is
declared PROPOSAL-ONLY (never auto-applies — a human decides). The router stays action_type-agnostic;
nothing in the existing endpoints was restructured.
"""
import asyncio
import unittest
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

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def post(self, url, **kwargs):
        return _FakeContextManager(_FakeResponse(200, "ok"))


class TestForestForkApprovalKind(unittest.TestCase):
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
        self._pool._approval_seq = 0
        self._redis.published.clear()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth_headers(self, user: str = "bobclaw"):
        return {"Authorization": f"Bearer {create_access_token(user)}"}

    # -- the kinds registry declares forest_fork as a proposal-only, human-decided kind --
    def test_kinds_registry_declares_forest_fork_proposal_only(self):
        resp = self._run(self._client.get("/approvals/kinds", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        by_kind = {k["action_type"]: k for k in data["kinds"]}
        self.assertIn("forest_fork", by_kind)
        ff = by_kind["forest_fork"]
        self.assertTrue(ff["proposal_only"])
        self.assertTrue(ff["requires_human"])
        self.assertTrue(ff["label"])
        # additive — the pre-existing kinds are still declared
        self.assertIn("task_approval", by_kind)
        self.assertIn("cc_edit", by_kind)

    def test_kinds_available_without_postgres(self):
        """Static registry: the kinds endpoint works even with no DB pool (does not call _get_pool)."""
        async def _exercise():
            client = TestClient(TestServer(build_app({POSTGRES_POOL_KEY: None})))
            await client.start_server()
            try:
                resp = await client.get("/approvals/kinds", headers=self._auth_headers())
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertIn("forest_fork", {k["action_type"] for k in data["kinds"]})
            finally:
                await client.close()

        self._run(_exercise())

    # -- a forest_fork proposal flows through the existing surface, proposal-only --
    def test_forest_fork_approval_lists_and_gets_with_rationale(self):
        a = self._pool.add_approval(
            approval_id=str(uuid4()),
            user_id="bobclaw",
            action_type="forest_fork",
            details={
                "fork_id": "fork:abc123",
                "parent_program": "bobpipe-uplift",
                "child_program_id": "slotA-deep-dive",
                "rationale": "the slotA question outgrew its parent tree",
                "seed_key": "forkseed:sha256:deadbeef",
            },
        )
        # lists (default pending) with the forest_fork action_type + rationale preserved
        resp = self._run(self._client.get("/approvals", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        items = self._run(resp.json())["items"]
        item = next(i for i in items if i["id"] == a["id"])
        self.assertEqual(item["action_type"], "forest_fork")
        self.assertEqual(item["details"]["rationale"], "the slotA question outgrew its parent tree")
        # detail view carries the verifiable seed key
        resp = self._run(self._client.get(f"/approvals/{a['id']}", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        detail = self._run(resp.json())
        self.assertEqual(detail["details"]["seed_key"], "forkseed:sha256:deadbeef")

    def test_forest_fork_is_proposal_only_until_decided(self):
        """Proposal-only (inv. 14): the row is pending until an explicit human decision applies it."""
        a = self._pool.add_approval(
            approval_id=str(uuid4()), user_id="bobclaw", action_type="forest_fork",
            details={"rationale": "why it can't stay a subtree"},
        )
        self.assertEqual(self._pool.approvals[a["id"]]["status"], "pending")  # nothing auto-applied

        with patch("routers.approvals.aiohttp.ClientSession", _FakeClientSession):
            resp = self._run(self._client.post(
                f"/approvals/{a['id']}/decide",
                json={"decision": "approve"},
                headers=self._auth_headers(),
            ))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._run(resp.json())["status"], "approved")
        self.assertEqual(self._pool.approvals[a["id"]]["status"], "approved")

    def test_forest_fork_reject_is_recorded(self):
        a = self._pool.add_approval(
            approval_id=str(uuid4()), user_id="bobclaw", action_type="forest_fork",
        )
        with patch("routers.approvals.aiohttp.ClientSession", _FakeClientSession):
            resp = self._run(self._client.post(
                f"/approvals/{a['id']}/decide",
                json={"decision": "reject"},
                headers=self._auth_headers(),
            ))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._pool.approvals[a["id"]]["status"], "rejected")

    def test_kinds_requires_jwt(self):
        resp = self._run(self._client.get("/approvals/kinds"))
        self.assertEqual(resp.status, 401)


if __name__ == "__main__":
    unittest.main()
