"""In-memory fake of redis.asyncio for tests.

Records publishes so tests can assert on them. Pub/sub subscriber
support is minimal (sufficient for unit tests that don't exercise the
WS subscriber path).
"""
import asyncio
from typing import Any


class FakeRedis:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    async def publish(self, channel: str, message: Any) -> int:
        self.published.append((channel, message))
        queues = self._subscribers.get(channel, [])
        for queue in queues:
            await queue.put({"type": "message", "channel": channel, "data": message})
        return len(queues)

    def pubsub(self) -> "FakePubSub":
        return FakePubSub(self)

    async def aclose(self) -> None:
        pass


class FakePubSub:
    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._subscribed: list[str] = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def subscribe(self, channel: str) -> None:
        self._subscribed.append(channel)
        self._redis._subscribers.setdefault(channel, []).append(self._queue)

    async def unsubscribe(self, channel: str) -> None:
        if channel in self._subscribed:
            self._subscribed.remove(channel)
        subs = self._redis._subscribers.get(channel, [])
        if self._queue in subs:
            subs.remove(self._queue)

    async def listen(self):
        while True:
            message = await self._queue.get()
            yield message

    async def get_message(self, ignore_subscribe_messages: bool = False,
                          timeout: float | None = None):
        """Mirror redis.asyncio PubSub.get_message: return the next message, or None
        if none arrives within ``timeout`` (the router polls this way so a redis-py 8.x
        idle read-timeout can't kill the forward loop). The fake queue only ever holds
        real ``{"type":"message",...}`` frames, so ignore_subscribe_messages is a no-op."""
        try:
            if timeout is None:
                return await self._queue.get()
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    async def aclose(self) -> None:
        for channel in list(self._subscribed):
            await self.unsubscribe(channel)
