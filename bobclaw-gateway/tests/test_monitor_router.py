"""Flight substrate L0.3 — /ws/monitor gateway transport.

Proves the read-only monitor WS mirrors /ws/approvals:
  * auth-middleware exempt (does its own first-frame auth — a middleware 401 would
    pre-empt the upgrade);
  * a frame published to `bobclaw:monitor` (core.telemetry.emit's channel) is forwarded
    verbatim to a subscribed watcher;
  * an optional `?flight_id=` narrows the stream to one flight;
  * a scoped AGENT token is rejected (allow_agent=False) — the fleet is a human surface.
"""
import asyncio
import json
import unittest

from aiohttp.test_utils import TestClient, TestServer

from app_state import POSTGRES_POOL_KEY
from auth import create_access_token, create_agent_token
from gateway import build_app
from tests.fake_pool import InMemoryPostgresPool
from tests.fake_redis import FakeRedis
import redis_client

MONITOR_CHANNEL = "bobclaw:monitor"


class TestMonitorRouter(unittest.TestCase):
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
        self._redis.published.clear()

    def _run(self, coro):
        return self._loop.run_until_complete(coro)

    def _auth_headers(self, user: str = "bobclaw"):
        return {"Authorization": f"Bearer {create_access_token(user)}"}

    # ── auth-exemption ────────────────────────────────────────────────────────
    def test_ws_monitor_is_auth_exempt(self):
        ws = self._run(self._client.ws_connect("/ws/monitor"))
        self.assertFalse(ws.closed)  # upgrade succeeded, not pre-401'd by middleware
        self._run(ws.close())

    # ── forwarding ────────────────────────────────────────────────────────────
    def test_ws_monitor_forwards_published_frame(self):
        async def _body():
            ws = await self._client.ws_connect("/ws/monitor", headers=self._auth_headers())
            await asyncio.sleep(0.05)  # let the handler auth + subscribe
            frame = {"type": "worker_state", "flight_id": "ms-5", "idx": 3, "status": "running"}
            await self._redis.publish(MONITOR_CHANNEL, json.dumps(frame))
            got = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            await ws.close()
            return got

        got = self._run(_body())
        self.assertEqual(got["type"], "worker_state")
        self.assertEqual(got["flight_id"], "ms-5")
        self.assertEqual(got["idx"], 3)

    def test_ws_monitor_flight_filter(self):
        async def _body():
            ws = await self._client.ws_connect(
                "/ws/monitor?flight_id=ms-5", headers=self._auth_headers()
            )
            await asyncio.sleep(0.05)
            # A frame for a DIFFERENT flight must be dropped; the matching one delivered.
            await self._redis.publish(
                MONITOR_CHANNEL, json.dumps({"type": "worker_state", "flight_id": "other", "idx": 1})
            )
            await self._redis.publish(
                MONITOR_CHANNEL, json.dumps({"type": "worker_state", "flight_id": "ms-5", "idx": 2})
            )
            got = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            await ws.close()
            return got

        got = self._run(_body())
        # First delivered frame is the ms-5 one (the "other" frame was filtered out).
        self.assertEqual(got["flight_id"], "ms-5")
        self.assertEqual(got["idx"], 2)

    # ── agent-token rejection ─────────────────────────────────────────────────
    def test_ws_monitor_rejects_agent_token(self):
        async def _body():
            token = create_agent_token("bobclaw", faces=["assistant"])
            ws = await self._client.ws_connect(
                "/ws/monitor", headers={"Authorization": f"Bearer {token}"}
            )
            # authenticate_ws sends {type:error, code:forbidden} then closes.
            msg = await asyncio.wait_for(ws.receive_json(), timeout=2.0)
            await ws.close()
            return msg

        msg = self._run(_body())
        self.assertEqual(msg["type"], "error")
        self.assertEqual(msg["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
