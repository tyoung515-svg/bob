import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool


class TestIdeasRoutes(unittest.TestCase):
    _loop: asyncio.AbstractEventLoop
    _client: TestClient
    _pool: InMemoryPostgresPool

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            cls._pool = InMemoryPostgresPool()
            cls._client = TestClient(
                TestServer(build_app({POSTGRES_POOL_KEY: cls._pool}))
            )
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.close()

    def setUp(self) -> None:
        self._pool.ideas.clear()
        self._pool._idea_seq = 0

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth_headers(self, user: str = "bobclaw"):
        token = create_access_token(user)
        return {"Authorization": f"Bearer {token}"}

    def test_create_idea(self):
        resp = self._run(
            self._client.post(
                "/ideas",
                json={"body": "Try react-grid-layout for tile drag", "tags": ["ui", "v2"]},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 201)
        data = self._run(resp.json())
        self.assertEqual(data["body"], "Try react-grid-layout for tile drag")
        self.assertEqual(data["tags"], ["ui", "v2"])
        self.assertEqual(data["state"], "raw")
        self.assertEqual(data["user_id"], "bobclaw")
        self.assertIsNone(data["promoted_to"])

    def test_create_idea_requires_body(self):
        resp = self._run(
            self._client.post(
                "/ideas",
                json={"body": "   ", "tags": []},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_create_idea_rejects_non_array_tags(self):
        resp = self._run(
            self._client.post(
                "/ideas",
                json={"body": "ok", "tags": "not-an-array"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_list_ideas_returns_newest_first(self):
        older = self._pool.add_idea(
            body="Older idea",
            user_id="bobclaw",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        newer = self._pool.add_idea(
            body="Newer idea",
            user_id="bobclaw",
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        resp = self._run(
            self._client.get("/ideas?limit=20&offset=0", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [newer["id"], older["id"]])

    def test_list_ideas_filter_by_state(self):
        raw = self._pool.add_idea(body="raw idea", user_id="bobclaw", state="raw")
        parked = self._pool.add_idea(body="parked idea", user_id="bobclaw", state="parked")
        self._pool.add_idea(body="active idea", user_id="bobclaw", state="active")

        resp = self._run(
            self._client.get("/ideas?state=raw", headers=self._auth_headers())
        )
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [raw["id"]])

        resp = self._run(
            self._client.get("/ideas?state=parked", headers=self._auth_headers())
        )
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [parked["id"]])

    def test_list_ideas_invalid_state_rejected(self):
        resp = self._run(
            self._client.get("/ideas?state=bogus", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 400)

    def test_get_idea(self):
        idea = self._pool.add_idea(body="solo idea", user_id="bobclaw")
        resp = self._run(
            self._client.get(f"/ideas/{idea['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["id"], idea["id"])

    def test_get_missing_idea_404(self):
        resp = self._run(
            self._client.get("/ideas/nope", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

    def test_patch_idea_partial_body(self):
        idea = self._pool.add_idea(body="original", user_id="bobclaw", tags=["v1"])
        resp = self._run(
            self._client.patch(
                f"/ideas/{idea['id']}",
                json={"body": "updated"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["body"], "updated")
        self.assertEqual(data["tags"], ["v1"])  # unchanged

    def test_patch_idea_state_transition(self):
        idea = self._pool.add_idea(body="needs triage", user_id="bobclaw", state="raw")
        resp = self._run(
            self._client.patch(
                f"/ideas/{idea['id']}",
                json={"state": "triaged"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["state"], "triaged")

    def test_patch_idea_invalid_state_rejected(self):
        idea = self._pool.add_idea(body="x", user_id="bobclaw")
        resp = self._run(
            self._client.patch(
                f"/ideas/{idea['id']}",
                json={"state": "exploded"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_patch_idea_empty_body_rejected(self):
        idea = self._pool.add_idea(body="x", user_id="bobclaw")
        resp = self._run(
            self._client.patch(
                f"/ideas/{idea['id']}",
                json={"body": "   "},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_archive_idea_via_delete(self):
        idea = self._pool.add_idea(body="bye", user_id="bobclaw")
        resp = self._run(
            self._client.delete(f"/ideas/{idea['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)

        # archived ideas hidden from default list
        resp = self._run(self._client.get("/ideas", headers=self._auth_headers()))
        data = self._run(resp.json())
        self.assertEqual(data["items"], [])

        # but visible when explicitly filtered
        resp = self._run(
            self._client.get("/ideas?state=archived", headers=self._auth_headers())
        )
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [idea["id"]])

    def test_double_archive_404(self):
        idea = self._pool.add_idea(body="x", user_id="bobclaw", state="archived")
        resp = self._run(
            self._client.delete(f"/ideas/{idea['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

    def test_by_state_grouping(self):
        self._pool.add_idea(body="r1", user_id="bobclaw", state="raw")
        self._pool.add_idea(body="r2", user_id="bobclaw", state="raw")
        self._pool.add_idea(body="t1", user_id="bobclaw", state="triaged")
        self._pool.add_idea(body="archived", user_id="bobclaw", state="archived")

        resp = self._run(
            self._client.get("/ideas/by-state", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["raw"]["count"], 2)
        self.assertEqual(data["triaged"]["count"], 1)
        self.assertEqual(data["active"]["count"], 0)
        self.assertEqual(data["parked"]["count"], 0)
        self.assertNotIn("archived", data)
        self.assertEqual(len(data["raw"]["recent"]), 2)

    def test_cross_user_idea_isolation(self):
        intruder = self._pool.add_idea(body="other user", user_id="intruder")
        mine = self._pool.add_idea(body="mine", user_id="bobclaw")

        # List shows only mine
        resp = self._run(self._client.get("/ideas", headers=self._auth_headers()))
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [mine["id"]])

        # GET other user's → 404
        resp = self._run(
            self._client.get(f"/ideas/{intruder['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

        # PATCH other user's → 404
        resp = self._run(
            self._client.patch(
                f"/ideas/{intruder['id']}",
                json={"body": "hacked"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 404)

        # DELETE other user's → 404
        resp = self._run(
            self._client.delete(f"/ideas/{intruder['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)
