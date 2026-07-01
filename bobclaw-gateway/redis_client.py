"""
BoBClaw Gateway — Redis client (pub/sub for live notifications)

Lazy-initialised, failure-tolerant. Used today for the approvals
dashboard tile via channel `bobclaw:approvals:{user_id}`.
"""
import logging
from typing import Any, Optional

import redis.asyncio as aioredis

from config import config

logger = logging.getLogger(__name__)

_redis_client: Optional[Any] = None


def get_redis() -> Any:
    """Return the module-level Redis client (lazy-init)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.from_url(config.REDIS_URL, decode_responses=True)
    return _redis_client


def set_redis_client(client: Any) -> None:
    """Inject a Redis client (used by tests with a fake)."""
    global _redis_client
    _redis_client = client


async def close_redis() -> None:
    global _redis_client
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception as exc:
            logger.warning("Redis close failed: %s", exc)
        _redis_client = None
