import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool


class TestProjectsRoutes(unittest.TestCase):
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
        self._pool.projects.clear()
        self._pool._conversation_seq = 0
        self._pool._message_seq = 0
        self._pool._project_seq = 0

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth_headers(self):
        token = create_access_token("bobclaw")
        return {"Authorization": f"Bearer {token}"}

    def test_create_project(self):
        resp = self._run(
            self._client.post(
                "/projects",
                json={
                    "name": "Forest OS",
                    "description": "Ecosystem work",
                    "instructions": "Always cite sources.",
                    "default_face_id": "researcher",
                    "default_backend": "deepseek_v4_flash",
                },
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 201)
        data = self._run(resp.json())
        self.assertEqual(data["name"], "Forest OS")
        self.assertEqual(data["description"], "Ecosystem work")
        self.assertEqual(data["instructions"], "Always cite sources.")
        self.assertEqual(data["default_face_id"], "researcher")
        self.assertEqual(data["default_backend"], "deepseek_v4_flash")
        self.assertFalse(data["is_archived"])

    def test_create_project_blank_name_rejected(self):
        resp = self._run(
            self._client.post(
                "/projects",
                json={"name": "   "},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_list_projects_with_conversation_count(self):
        project = self._pool.add_project(name="SIOP", user_id="bobclaw")
        # Two member conversations (one archived → must not count).
        self._pool.add_conversation(title="A", user_id="bobclaw", project_id=project["id"])
        self._pool.add_conversation(title="B", user_id="bobclaw", project_id=project["id"])
        self._pool.add_conversation(
            title="Archived", user_id="bobclaw", project_id=project["id"], is_archived=True
        )

        resp = self._run(self._client.get("/projects", headers=self._auth_headers()))
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["id"], project["id"])
        self.assertEqual(item["conversation_count"], 2)
        # List is intentionally light — no instructions field.
        self.assertNotIn("instructions", item)

    def test_get_project_includes_instructions(self):
        project = self._pool.add_project(
            name="LKS", user_id="bobclaw", instructions="Local-first, API over GUI."
        )
        resp = self._run(
            self._client.get(f"/projects/{project['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["instructions"], "Local-first, API over GUI.")

    def test_get_missing_project_returns_404(self):
        resp = self._run(
            self._client.get("/projects/does-not-exist", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

    def test_update_project_name_and_instructions(self):
        project = self._pool.add_project(name="Old Name", user_id="bobclaw")
        resp = self._run(
            self._client.post(
                f"/projects/{project['id']}",
                json={"name": "New Name", "instructions": "Updated context."},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["name"], "New Name")
        self.assertEqual(data["instructions"], "Updated context.")

    def test_update_project_blank_name_rejected(self):
        project = self._pool.add_project(name="Keep", user_id="bobclaw")
        resp = self._run(
            self._client.post(
                f"/projects/{project['id']}",
                json={"name": "  "},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_delete_project_archives_and_unassigns_conversations(self):
        project = self._pool.add_project(name="Doomed", user_id="bobclaw")
        member = self._pool.add_conversation(
            title="Member", user_id="bobclaw", project_id=project["id"]
        )

        resp = self._run(
            self._client.delete(f"/projects/{project['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["status"], "archived")
        self.assertEqual(data["project_id"], project["id"])

        # Project no longer listed.
        resp = self._run(self._client.get("/projects", headers=self._auth_headers()))
        listed = self._run(resp.json())
        self.assertEqual(listed["items"], [])

        # Member conversation's project_id is cleared.
        self.assertIsNone(self._pool.conversations[member["id"]]["project_id"])

    def test_create_conversation_inherits_project_defaults(self):
        project = self._pool.add_project(
            name="Inheritor",
            user_id="bobclaw",
            default_face_id="reviewer",
            default_backend="kimi_code",
        )
        resp = self._run(
            self._client.post(
                "/conversations",
                json={"title": "Child", "project_id": project["id"]},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 201)
        data = self._run(resp.json())
        self.assertEqual(data["project_id"], project["id"])
        self.assertEqual(data["face_id"], "reviewer")
        self.assertEqual(data["backend_preference"], "kimi_code")

    def test_create_conversation_explicit_face_overrides_project_default(self):
        project = self._pool.add_project(
            name="Inheritor",
            user_id="bobclaw",
            default_face_id="reviewer",
            default_backend="kimi_code",
        )
        resp = self._run(
            self._client.post(
                "/conversations",
                json={"title": "Child", "project_id": project["id"], "face_id": "assistant"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 201)
        data = self._run(resp.json())
        self.assertEqual(data["face_id"], "assistant")
        # Backend still follows the project default.
        self.assertEqual(data["backend_preference"], "kimi_code")

    def test_create_conversation_unknown_project_rejected(self):
        resp = self._run(
            self._client.post(
                "/conversations",
                json={"title": "Orphan", "project_id": "no-such-project"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_assign_and_unassign_conversation(self):
        project = self._pool.add_project(name="Bucket", user_id="bobclaw")
        conversation = self._pool.add_conversation(title="Floating", user_id="bobclaw")

        # Assign
        resp = self._run(
            self._client.post(
                f"/conversations/{conversation['id']}/project",
                json={"project_id": project["id"]},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["project_id"], project["id"])

        # Unassign (null)
        resp = self._run(
            self._client.post(
                f"/conversations/{conversation['id']}/project",
                json={"project_id": None},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertIsNone(data["project_id"])

    def test_assign_unknown_project_rejected(self):
        conversation = self._pool.add_conversation(title="Floating", user_id="bobclaw")
        resp = self._run(
            self._client.post(
                f"/conversations/{conversation['id']}/project",
                json={"project_id": "no-such-project"},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 400)

    def test_assign_missing_conversation_returns_404(self):
        project = self._pool.add_project(name="Bucket", user_id="bobclaw")
        resp = self._run(
            self._client.post(
                "/conversations/no-such-conv/project",
                json={"project_id": project["id"]},
                headers=self._auth_headers(),
            )
        )
        self.assertEqual(resp.status, 404)

    def test_project_id_present_in_conversation_responses(self):
        project = self._pool.add_project(name="Visible", user_id="bobclaw")
        conversation = self._pool.add_conversation(
            title="WithProject", user_id="bobclaw", project_id=project["id"]
        )

        # get_conversation
        resp = self._run(
            self._client.get(
                f"/conversations/{conversation['id']}", headers=self._auth_headers()
            )
        )
        self.assertEqual(resp.status, 200)
        data = self._run(resp.json())
        self.assertEqual(data["project_id"], project["id"])
        self.assertIn("backend_preference", data)

        # list_conversations
        resp = self._run(self._client.get("/conversations", headers=self._auth_headers()))
        listed = self._run(resp.json())
        self.assertEqual(listed["items"][0]["project_id"], project["id"])

    def test_cross_user_project_isolation(self):
        other = self._pool.add_project(name="Other User's", user_id="intruder")
        mine = self._pool.add_project(name="Mine", user_id="bobclaw")

        # List shows only mine.
        resp = self._run(self._client.get("/projects", headers=self._auth_headers()))
        data = self._run(resp.json())
        self.assertEqual([item["id"] for item in data["items"]], [mine["id"]])

        # Get other user's project → 404
        resp = self._run(
            self._client.get(f"/projects/{other['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)

        # Delete other user's project → 404
        resp = self._run(
            self._client.delete(f"/projects/{other['id']}", headers=self._auth_headers())
        )
        self.assertEqual(resp.status, 404)
