"""Redis-backed sliding window rate limiter for FastAPI."""

import time
import uuid
from typing import Any

from fastapi import Depends, HTTPException, Request, status

from app.redis import get_redis


def _extract_user_id(request: Request) -> str | None:
    """Extract user ID from JWT Bearer token in the Authorization header.

    Returns the user ID string or None if the token is missing/invalid.
    Does not raise - rate limiting is silently skipped for unauthenticated
    requests when by="user".
    """
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return None
    try:
        from app.auth.jwt import decode_token

        payload = decode_token(auth[7:])
        user_id: str | None = payload.get("sub")
        return user_id
    except Exception:
        return None


def rate_limit(
    limit: int,
    window: int = 3600,
    by: str = "ip",
) -> Any:
    """Create a rate limit dependency.

    Args:
        limit: Maximum number of requests allowed in the window.
        window: Time window in seconds (default 1 hour).
        by: Key type - "ip" for client IP, "user" for authenticated user ID.
    """

    async def _check_rate_limit(request: Request) -> None:
        redis = get_redis()
        now = time.time()
        window_start = now - window

        # Build the rate limit key
        if by == "user":
            identifier = _extract_user_id(request)
            if not identifier:
                return
        else:
            identifier = request.client.host if request.client else "unknown"

        key = f"rl:{request.url.path}:{identifier}"

        # Sliding window using a Redis sorted set:
        # 1. Remove expired entries
        # 2. Count current entries
        # 3. If under limit, add new entry
        pipe = redis.pipeline()
        pipe.zremrangebyscore(key, 0, window_start)
        pipe.zcard(key)
        results: list[Any] = await pipe.execute()
        current_count: int = results[1]

        if current_count >= limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded",
                headers={"Retry-After": str(window)},
            )

        # Add this request (unique member, scored by timestamp)
        await redis.zadd(key, {str(uuid.uuid4()): now})
        await redis.expire(key, window)

    return Depends(_check_rate_limit)
