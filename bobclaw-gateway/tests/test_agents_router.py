import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool


class TestAgentsRoutes(unittest.TestCase):
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
        self._pool.conversations.clear()
        self._pool.messages.clear()
        self._pool.agent_bindings.clear()
        self._pool.channel_bindings.clear()
        self._pool._conversation_seq = 0
        self._pool._message_seq = 0
        self._pool._agent_binding_seq = 0

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth_headers(self):
        token = create_access_token("bobclaw")
        return {"Authorization": f"Bearer {token}"}

    def _create_agent(self, slug="research-bot", face_id="researcher", **extra):
        return self._run(
            self._client.post(
                "/agents",
                json={"slug": slug, "face_id": face_id, **extra},
                headers=self._auth_headers(),
            )
        )

    def test_create_get_list_agent(self):
        resp = self._create_agent(display_name="Research Bot", profile_name="work")
        self.assertEqual(resp.status, 201)
        created = self._run(resp.json())
        self.assertEqual(created["slug"], "research-bot")
        self.assertEqual(created["display_name"], "Research Bot")
        self.assertEqual(created["face_id"], "researcher")
        self.assertEqual(created["profile_name"], "work")
        self.assertTrue(created["conversation_id"])
        # Face registry join: display-only fields are populated.
        self.assertTrue(created["avatar"])
        self.assertTrue(created["face_name"])

        # The canonical conversation was created with the display-only title.
        conversation = self._pool.conversations[created["conversation_id"]]
        self.assertEqual(conversation["title"], "Bot: research-bot")
        self.assertEqual(conversation["face_id"], "researcher")
        self.assertEqual(conversation["user_id"], "bobclaw")

        resp = self._run(self._client.get("/agents/research-bot", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        fetched = self._run(resp.json())
        self.assertEqual(fetched["id"], created["id"])
        self.assertEqual(fetched["conversation_id"], created["conversation_id"])

        resp = self._run(self._client.get("/agents", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual([item["slug"] for item in data["items"]], ["research-bot"])

    def test_create_display_name_defaults_to_slug(self):
        resp = self._create_agent()
        self.assertEqual(resp.status, 201)
        data = self._run(resp.json())
        self.assertEqual(data["display_name"], "research-bot")
        self.assertIsNone(data["profile_name"])

    def test_create_slug_conflict_returns_409(self):
        resp = self._create_agent()
        self.assertEqual(resp.status, 201)

        resp = self._create_agent()
        self.assertEqual(resp.status, 409)

        # Transactional create: the conflict short-circuits before any insert,
        # so no second conversation or binding row exists.
        self.assertEqual(len(self._pool.conversations), 1)
        self.assertEqual(len(self._pool.agent_bindings), 1)

    def test_create_unknown_face_returns_400(self):
        resp = self._create_agent(face_id="no-such-face")
        self.assertEqual(resp.status, 400)
        self.assertEqual(len(self._pool.conversations), 0)
        self.assertEqual(len(self._pool.agent_bindings), 0)

    def test_create_missing_required_fields_returns_400(self):
        resp = self._create_agent(slug="")
        self.assertEqual(resp.status, 400)
        resp = self._create_agent(face_id="")
        self.assertEqual(resp.status, 400)

    def test_cross_user_agent_isolation(self):
        """A user must not see or modify another user's agent bindings."""
        other_conv = self._pool.add_conversation(title="Bot: theirs", user_id="intruder")
        self._pool.add_agent_binding(
            slug="their-bot",
            face_id="researcher",
            user_id="intruder",
            conversation_id=other_conv["id"],
        )
        mine = self._create_agent(slug="my-bot")
        self.assertEqual(mine.status, 201)

        # List only shows mine.
        resp = self._run(self._client.get("/agents", headers=self._auth_headers()))
        data = self._run(resp.json())
        self.assertEqual([item["slug"] for item in data["items"]], ["my-bot"])

        # Get other user's binding → 404 (not-found and not-owned both 404).
        resp = self._run(self._client.get("/agents/their-bot", headers=self._auth_headers()))
        self.assertEqual(resp.status, 404)

        # Delete other user's binding → 404, and it stays active.
        resp = self._run(self._client.delete("/agents/their-bot", headers=self._auth_headers()))
        self.assertEqual(resp.status, 404)
        other = next(iter(self._pool.agent_bindings.values()))
        self.assertFalse(other["is_archived"])
        self.assertFalse(self._pool.conversations[other_conv["id"]]["is_archived"])

        # Unknown slug → 404 as well.
        resp = self._run(self._client.get("/agents/never-existed", headers=self._auth_headers()))
        self.assertEqual(resp.status, 404)

    def test_delete_archives_binding_and_conversation(self):
        resp = self._create_agent()
        self.assertEqual(resp.status, 201)
        created = self._run(resp.json())

        resp = self._run(self._client.delete("/agents/research-bot", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["status"], "archived")
        self.assertEqual(data["conversation_id"], created["conversation_id"])

        binding = next(iter(self._pool.agent_bindings.values()))
        self.assertTrue(binding["is_archived"])
        self.assertTrue(self._pool.conversations[created["conversation_id"]]["is_archived"])

        # Archived binding disappears from list/get.
        resp = self._run(self._client.get("/agents", headers=self._auth_headers()))
        self.assertEqual(self._run(resp.json())["items"], [])
        resp = self._run(self._client.get("/agents/research-bot", headers=self._auth_headers()))
        self.assertEqual(resp.status, 404)

        # Second delete → 404.
        resp = self._run(self._client.delete("/agents/research-bot", headers=self._auth_headers()))
        self.assertEqual(resp.status, 404)

    def test_recreate_after_delete_returns_409(self):
        """Explicit archive policy: archived slugs stay reserved (no auto-restore)."""
        resp = self._create_agent()
        self.assertEqual(resp.status, 201)
        resp = self._run(self._client.delete("/agents/research-bot", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)

        resp = self._create_agent()
        self.assertEqual(resp.status, 409)
        body = self._run(resp.text())
        self.assertIn("archived", body)

        # The failed re-create inserted nothing.
        self.assertEqual(len(self._pool.conversations), 1)
        self.assertEqual(len(self._pool.agent_bindings), 1)
