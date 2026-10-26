"""Worker heartbeat: renews leases for in-flight steps and detects revocation."""

from __future__ import annotations

import os
import socket
import uuid

from agentos_core.engine.core import HeartbeatResult, heartbeat


async def send_heartbeat(
    worker_id: str,
    tokens: list[uuid.UUID],
    *,
    concurrency: int,
    lease_ttl: float,
    steps_completed: int | None = None,
) -> HeartbeatResult:
    return await heartbeat(
        worker_id,
        tokens,
        hostname=socket.gethostname(),
        pid=os.getpid(),
        concurrency=concurrency,
        lease_ttl=lease_ttl,
        steps_completed=steps_completed,
    )
