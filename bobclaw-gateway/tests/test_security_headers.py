"""Security response headers (convergence debt H — port from release repo v0.98.1).

Proves the security-headers middleware stamps a conservative header set on EVERY
response, including the auth 401s raised by inner middleware (it is registered
outermost precisely so error responses are covered too).
"""
import asyncio
import unittest

from aiohttp.test_utils import TestClient, TestServer

from gateway import build_app

_EXPECTED = {
    "Content-Security-Policy",
    "X-Content-Type-Options",
    "X-Frame-Options",
    "Referrer-Policy",
}


class _Base(unittest.TestCase):
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


class TestSecurityHeaders(_Base):
    def test_headers_on_200(self):
        resp = self._run(self._client.get("/health"))
        self.assertEqual(resp.status, 200)
        for h in _EXPECTED:
            self.assertIn(h, resp.headers, f"missing {h} on 200")
        # CSP is the load-bearing one — confirm the strict script-src.
        self.assertIn("script-src 'self'", resp.headers["Content-Security-Policy"])
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")

    def test_headers_on_401_error_response(self):
        # A wrong-password login is a known 401 raised by the auth layer; the outermost
        # security middleware must still stamp it (the whole reason it is outermost).
        resp = self._run(
            self._client.post("/auth/login", json={"password": "definitely-wrong"})
        )
        self.assertEqual(resp.status, 401)
        for h in _EXPECTED:
            self.assertIn(h, resp.headers, f"missing {h} on 401")


if __name__ == "__main__":
    unittest.main()
