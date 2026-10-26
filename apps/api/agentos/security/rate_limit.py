"""Fixed-window rate limiting in Redis, with a per-process fallback if Redis is down."""

from __future__ import annotations

import time
from collections import defaultdict

from agentos_core.notify import get_notifier

_local: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))


async def hit(key: str, limit: int, window_seconds: int = 60) -> tuple[bool, int]:
    """Count one request. Returns (allowed, retry_after_seconds)."""
    now = int(time.time())
    window = now // window_seconds
    bucket = f"agentos:rl:{key}:{window}"
    retry_after = window_seconds - (now % window_seconds)
    try:
        client = get_notifier().client
        async with client.pipeline(transaction=True) as pipe:
            pipe.incr(bucket)
            pipe.expire(bucket, window_seconds + 1)
            count, _ = await pipe.execute()
    except Exception:
        w, c = _local[key]
        count = c + 1 if w == window else 1
        _local[key] = (window, count)
    return int(count) <= limit, retry_after
