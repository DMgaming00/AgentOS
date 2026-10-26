"""Redis pub/sub used only to *accelerate* wake-ups.

Every consumer must tolerate missed or duplicated notifications: PostgreSQL rows
are authoritative and consumers always re-read state from the database.
If Redis is unreachable, publishing is a logged no-op and consumers fall back to polling.
"""

from __future__ import annotations

import contextlib
import json
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

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            self._client = aioredis.from_url(
                self._url, socket_connect_timeout=1.0, socket_timeout=2.0, decode_responses=True
            )
        return self._client

    async def publish(self, channel: str, message: dict[str, Any]) -> bool:
        if time.monotonic() < self._down_until:
            return False
        try:
            await self.client.publish(channel, json.dumps(message, default=str))
            return True
        except Exception as exc:
            # Back off for a few seconds so an outage does not slow every transaction.
            self._down_until = time.monotonic() + 5.0
            log.warning("redis_publish_failed", channel=channel, error=str(exc))
            return False

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
