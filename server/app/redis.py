"""Async Redis client lifecycle management."""

import redis.asyncio as aioredis

from app.config import get_settings

_client: aioredis.Redis | None = None


async def init_redis() -> None:
    """Create the async Redis connection."""
    global _client  # noqa: PLW0603
    settings = get_settings()
    _client = aioredis.from_url(settings.redis_url, decode_responses=True)


async def close_redis() -> None:
    """Close the Redis connection."""
    global _client  # noqa: PLW0603
    if _client:
        await _client.aclose()
        _client = None


def get_redis() -> aioredis.Redis:
    """Return the active Redis client. Raises if not initialized."""
    if _client is None:
        raise RuntimeError("Redis not initialized - call init_redis() first")
    return _client
