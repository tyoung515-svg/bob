import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool


class TestConversationsRoutes(unittest.TestCase):
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
        self._pool._conversation_seq = 0
        self._pool._message_seq = 0

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth_headers(self):
        token = create_access_token("bobclaw")
        return {"Authorization": f"Bearer {token}"}

    def test_create_conversation(self):
        resp = self._run(
            self._client.post(
                "/conversations",
                json={"title": "Project Alpha", "face_id": "sage", "model_preference": "gpt-5"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 201)
        data = self._run(resp.json())
        self.assertEqual(data["title"], "Project Alpha")
        self.assertEqual(data["face_id"], "sage")
        self.assertEqual(data["model_preference"], "gpt-5")

    def test_list_conversations_returns_newest_first(self):
        older = self._pool.add_conversation(
            title="Older",
            user_id="bobclaw",
            updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        newer = self._pool.add_conversation(
            title="Newer",
            user_id="bobclaw",
            updated_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        self._pool.add_message(conversation_id=newer["id"], role="assistant", content="Latest preview")
        self._pool.add_message(conversation_id=older["id"], role="assistant", content="Older preview")

        resp = self._run(
            self._client.get("/conversations?limit=20&offset=0", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual([item["title"] for item in data["items"]], ["Newer", "Older"])
        self.assertEqual(data["items"][0]["last_message_preview"], "Latest preview")

    def test_get_messages_with_cursor_pagination(self):
        conversation = self._pool.add_conversation(title="Cursor Test", user_id="bobclaw")
        now = datetime.now(timezone.utc)
        self._pool.add_message(
            conversation_id=conversation["id"], role="user", content="first", created_at=now - timedelta(minutes=3), message_id="msg-1"
        )
        self._pool.add_message(
            conversation_id=conversation["id"], role="assistant", content="second", created_at=now - timedelta(minutes=2), message_id="msg-2"
        )
        self._pool.add_message(
            conversation_id=conversation["id"], role="user", content="third", created_at=now - timedelta(minutes=1), message_id="msg-3"
        )

        resp = self._run(
            self._client.get(
                f"/conversations/{conversation['id']}/messages?limit=2",
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 200)
        first_page = self._run(resp.json())
        self.assertEqual([item["id"] for item in first_page["items"]], ["msg-3", "msg-2"])
        self.assertTrue(first_page["has_more"])

        resp = self._run(
            self._client.get(
                f"/conversations/{conversation['id']}/messages?limit=2&before=msg-2",
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 200)
        second_page = self._run(resp.json())
        self.assertEqual([item["id"] for item in second_page["items"]], ["msg-1"])
        self.assertFalse(second_page["has_more"])

    def test_soft_delete_hides_archived_conversations(self):
        archived = self._pool.add_conversation(title="Archive Me", user_id="bobclaw")
        visible = self._pool.add_conversation(title="Keep Me", user_id="bobclaw")

        resp = self._run(
            self._client.delete(
                f"/conversations/{archived['id']}",
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 200)

        resp = self._run(
            self._client.get("/conversations", headers=self._auth_headers())
        )
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [visible["id"]])

    def test_cross_user_conversation_isolation(self):
        """A user must not see or modify another user's conversations."""
        other = self._pool.add_conversation(title="Other User's", user_id="intruder")
        mine = self._pool.add_conversation(title="My Conversation", user_id="bobclaw")

        # List should only show mine
        resp = self._run(
            self._client.get("/conversations", headers=self._auth_headers())
        )
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [mine["id"]])

        # Get other user's conversation → 404
        resp = self._run(
            self._client.get(f"/conversations/{other['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

        # Archive other user's conversation → 404
        resp = self._run(
            self._client.delete(f"/conversations/{other['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

        # Rename other user's conversation → 404
        resp = self._run(
            self._client.post(
                f"/conversations/{other['id']}/rename",
                json={"title": "Hacked"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 404)
