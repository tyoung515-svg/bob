"""MS9-F6 — gateway approvals surface: the additive ``forest_experiment`` approval kind.

Pins the F6 gateway accept criterion: the approvals surface serves the new ``forest_experiment`` kind
additively (kinds registry + a ``forest_experiment`` proposal flows through list/get/decide), and it is
declared PROPOSAL-ONLY (never auto-applies — a human decides). Mirrors the F7 ``forest_fork`` test; the
router stays action_type-agnostic and nothing in the existing endpoints was restructured.
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


class TestForestExperimentApprovalKind(unittest.TestCase):
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

    # -- the kinds registry declares forest_experiment as proposal-only, human-decided --
    def test_kinds_registry_declares_forest_experiment_proposal_only(self):
        resp = self._run(self._client.get("/approvals/kinds", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        by_kind = {k["action_type"]: k for k in data["kinds"]}
        self.assertIn("forest_experiment", by_kind)
        fe = by_kind["forest_experiment"]
        self.assertTrue(fe["proposal_only"])
        self.assertTrue(fe["requires_human"])
        self.assertTrue(fe["label"])
        # additive — the pre-existing kinds are still declared
        self.assertIn("task_approval", by_kind)
        self.assertIn("cc_edit", by_kind)
        self.assertIn("forest_fork", by_kind)

    def test_kinds_available_without_postgres(self):
        """Static registry: the kinds endpoint works even with no DB pool (does not call _get_pool)."""
        async def _exercise():
            client = TestClient(TestServer(build_app({POSTGRES_POOL_KEY: None})))
            await client.start_server()
            try:
                resp = await client.get("/approvals/kinds", headers=self._auth_headers())
                self.assertEqual(resp.status, 200)
                data = await resp.json()
                self.assertIn(
                    "forest_experiment", {k["action_type"] for k in data["kinds"]}
                )
            finally:
                await client.close()

        self._run(_exercise())

    # -- a forest_experiment proposal flows through the existing surface, proposal-only --
    def test_forest_experiment_approval_lists_and_gets_with_budget_math(self):
        a = self._pool.add_approval(
            approval_id=str(uuid4()),
            user_id="bobclaw",
            action_type="forest_experiment",
            details={
                "experiment_id": "exp:abc123",
                "node_id": "h_uplift",
                "runner": "testpipe_uplift",
                "config": {"name": "B0", "worker": "deepseek_v4_flash"},
                "est_cost": 0.5,
                "state_mutating": False,
                "reason": "projected tree/epoch spend $2.4 exceeds $2.0 cap",
                "per_tree_epoch": 2.0,
                "per_day_forest": 5.0,
            },
        )
        # lists (default pending) with the forest_experiment action_type + reason preserved
        resp = self._run(self._client.get("/approvals", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        items = self._run(resp.json())["items"]
        item = next(i for i in items if i["id"] == a["id"])
        self.assertEqual(item["action_type"], "forest_experiment")
        self.assertIn("exceeds", item["details"]["reason"])
        # detail view carries the runner + est_cost
        resp = self._run(self._client.get(f"/approvals/{a['id']}", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        detail = self._run(resp.json())
        self.assertEqual(detail["details"]["runner"], "testpipe_uplift")
        self.assertEqual(detail["details"]["est_cost"], 0.5)

    def test_forest_experiment_is_proposal_only_until_decided(self):
        """Proposal-only (inv. 14): the row is pending until an explicit human decision applies it."""
        a = self._pool.add_approval(
            approval_id=str(uuid4()), user_id="bobclaw", action_type="forest_experiment",
            details={"reason": "state-mutating experiment always requires approval"},
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

    def test_forest_experiment_reject_is_recorded(self):
        a = self._pool.add_approval(
            approval_id=str(uuid4()), user_id="bobclaw", action_type="forest_experiment",
        )
        with patch("routers.approvals.aiohttp.ClientSession", _FakeClientSession):
            resp = self._run(self._client.post(
                f"/approvals/{a['id']}/decide",
                json={"decision": "reject"},
                headers=self._auth_headers(),
            ))
        self.assertEqual(resp.status, 200)
        self.assertEqual(self._pool.approvals[a["id"]]["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
