"""Redis pub/sub used only to *accelerate* wake-ups.

Every consumer must tolerate missed or duplicated notifications: PostgreSQL rows
are authoritative and consumers always re-read state from the database.
If Redis is unreachable, publishing is a logged no-op and consumers fall back to polling.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import random
import time
from typing import Any

import redis.asyncio as aioredis

from agentos_core.config import get_settings
from agentos_core.log import get_logger

log = get_logger(__name__)

WORK_CHANNEL = "agentos:work"
CANCEL_CHANNEL = "agentos:cancel"


def run_channel(run_id: object) -> str:
    return f"agentos:run:{run_id}"


class Notifier:
    def __init__(self, url: str | None = None) -> None:
        self._url = url or get_settings().redis_url
        self._client: aioredis.Redis | None = None
        self._down_until = 0.0
        # Chaos Lab hooks (inactive unless a fault fires): a refused-connection window and delayed delivery.
        self._outage_until = 0.0
        self._disorder_until = 0.0
        self._disorder_max_ms = 0
        self._disorder_rng = random.Random(0)
        self._delayed: set[asyncio.Task[None]] = set()

    async def inject_outage(self, seconds: float) -> None:
        """Drop every Redis connection and refuse new ones for ``seconds`` (a real interruption)."""
        self._outage_until = time.monotonic() + seconds
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.connection_pool.disconnect()

    def inject_disorder(self, *, seconds: float, max_delay_ms: int, seed: int) -> None:
        """Deliver publishes after a random delay, so notifications arrive late and out of order."""
        self._disorder_until = time.monotonic() + seconds
        self._disorder_max_ms = max_delay_ms
        self._disorder_rng = random.Random(seed)

    @property
    def client(self) -> aioredis.Redis:
        if time.monotonic() < self._outage_until:
            raise ConnectionError("chaos: Redis connection refused (injected outage)")
        if self._client is None:
            self._client = aioredis.from_url(
                self._url, socket_connect_timeout=1.0, socket_timeout=2.0, decode_responses=True
            )
        return self._client

    async def publish(self, channel: str, message: dict[str, Any]) -> bool:
        if time.monotonic() < self._down_until:
            return False
        if time.monotonic() < self._disorder_until and self._disorder_max_ms > 0:
            delay = self._disorder_rng.uniform(0, self._disorder_max_ms) / 1000
            task = asyncio.create_task(self._publish_later(delay, channel, message))
            self._delayed.add(task)
            task.add_done_callback(self._delayed.discard)
            return True
        try:
            await self.client.publish(channel, json.dumps(message, default=str))
            return True
        except Exception as exc:
            # Back off for a few seconds so an outage does not slow every transaction.
            self._down_until = time.monotonic() + 5.0
            log.warning("redis_publish_failed", channel=channel, error=str(exc))
            return False

    async def _publish_later(self, delay: float, channel: str, message: dict[str, Any]) -> None:
        await asyncio.sleep(delay)
        with contextlib.suppress(Exception):
            await self.client.publish(channel, json.dumps(message, default=str))

    async def ping(self) -> bool:
        try:
            return bool(await self.client.ping())
        except Exception:
            return False

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):  # best effort shutdown
                await self._client.aclose()
            self._client = None


_notifier: Notifier | None = None


def get_notifier() -> Notifier:
    global _notifier
    if _notifier is None:
        _notifier = Notifier()
    return _notifier


def set_notifier(n: Notifier | None) -> None:
    global _notifier
    _notifier = n
