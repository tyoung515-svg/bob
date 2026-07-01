"""
Tests for static web UI serving (T1a backend).

Verifies the gateway can serve bobclaw-gateway/ui/ at /ui tokenless, redirects
root to /ui/, and that this does NOT weaken auth on real API routes.

Covers:
 - GET /ui/<asset> → 200 without a token (login page loads before auth)
 - GET / → 302 with Location: /ui/ (no token)
 - GET /conversations → 401 without a token (over-exemption regression guard)
 - GET /ui/../auth.py traversal → contained (no source leak)

Mirrors test_gateway.py's shared-loop TestClient pattern.
"""
import asyncio
import pathlib
import unittest

from aiohttp.test_utils import TestClient, TestServer
from yarl import URL

import gateway as _gateway_mod
from gateway import build_app

_UI_DIR = pathlib.Path(_gateway_mod.__file__).parent / "ui"


class _UITestBase(unittest.TestCase):
    """Base: one shared event loop + TestClient per concrete subclass."""

    _loop: asyncio.AbstractEventLoop
    _client: TestClient

    @classmethod
    def setUpClass(cls) -> None:
        cls._loop = asyncio.new_event_loop()

        async def _setup() -> None:
            cls._client = TestClient(TestServer(build_app()))
            await cls._client.start_server()

        cls._loop.run_until_complete(_setup())

    @classmethod
    def tearDownClass(cls) -> None:
        cls._loop.run_until_complete(cls._client.close())
        cls._loop.close()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)


class TestStaticUIServing(_UITestBase):

    def test_ui_asset_served_without_token(self):
        # Assert on the backend-owned .gitkeep dir-marker (no ping.txt fixture
        # under ui/ — that bleeds into the T1b frontend lane). Empty body is
        # fine; the point is a tokenless 200 from the static route.
        resp = self._run(self._client.get("/ui/.gitkeep"))
        self.assertEqual(resp.status, 200)

    def test_root_redirects_to_ui(self):
        resp = self._run(self._client.get("/", allow_redirects=False))
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers.get("Location"), "/ui/")

    def test_bare_ui_redirects_to_slash(self):
        resp = self._run(self._client.get("/ui", allow_redirects=False))
        self.assertEqual(resp.status, 302)
        self.assertEqual(resp.headers.get("Location"), "/ui/")

    def test_ui_slash_serves_index_html_without_token(self):
        # add_static(show_index=False) 403s the bare dir "/ui/"; the explicit
        # _ui_index route must serve index.html. index.html is the frontend
        # lane's file (not in this worktree) — create a temp one for the test
        # and remove it after, leaving any real index.html untouched.
        index = _UI_DIR / "index.html"
        created = False
        if not index.exists():
            index.write_text(
                "<!doctype html><title>probe</title>", encoding="utf-8"
            )
            created = True
        try:
            resp = self._run(self._client.get("/ui/", allow_redirects=False))
            self.assertEqual(resp.status, 200)
            body = self._run(resp.text())
            self.assertIn("<!doctype html", body.lower())
        finally:
            if created:
                index.unlink()

    def test_protected_route_still_401_without_token(self):
        # Regression guard: exempting /ui must NOT exempt API routes.
        resp = self._run(self._client.get("/conversations"))
        self.assertEqual(resp.status, 401)

    def test_path_traversal_is_contained(self):
        # add_static blocks ../ escapes; must not leak gateway source.
        # Send the PERCENT-ENCODED form via encoded=True so the client does
        # NOT normalize "/ui/../auth.py" -> "/auth.py" before it reaches the
        # server (the plain form passes trivially without exercising the guard).
        resp = self._run(
            self._client.get(
                URL("/ui/..%2fauth.py", encoded=True), allow_redirects=False
            )
        )
        self.assertIn(resp.status, (400, 403, 404))
        if resp.status == 200:  # defensive: never serve source
            body = self._run(resp.text())
            self.assertNotIn("auth_middleware", body)


if __name__ == "__main__":
    unittest.main()
