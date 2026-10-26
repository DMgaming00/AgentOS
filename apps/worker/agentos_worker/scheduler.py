"""Worker scheduling loop: claim -> execute (bounded concurrency) -> heartbeat."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import uuid
from dataclasses import dataclass, field

from agentos_core.config import get_settings
from agentos_core.engine.core import (
    ClaimedStep,
    StaleLeaseError,
    claim_steps,
    mark_step_cancelled,
    mark_worker_stopped,
)
from agentos_core.log import get_logger
from agentos_core.notify import CANCEL_CHANNEL, WORK_CHANNEL, get_notifier
from agentos_core.storage import Storage
from agentos_core.tools.registry import get_registry
from agentos_worker.executor import StepExecutor
from agentos_worker.heartbeat import send_heartbeat
from agentos_worker.recovery import coordinator_tick

log = get_logger(__name__)


@dataclass
class InFlight:
    claim: ClaimedStep
    task: asyncio.Task[None]
    cancel_reason: str | None = None


@dataclass
class WorkerStats:
    claimed: int = 0
    outcomes: dict[str, int] = field(default_factory=dict)


class Worker:
    def __init__(
        self,
        worker_id: str | None = None,
        *,
        concurrency: int | None = None,
        lease_ttl: float | None = None,
        heartbeat_interval: float | None = None,
        poll_interval: float | None = None,
        run_coordinator: bool = True,
    ) -> None:
        s = get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.concurrency = concurrency or s.worker_concurrency
        self.lease_ttl = lease_ttl or s.lease_ttl_seconds
        self.heartbeat_interval = heartbeat_interval or s.heartbeat_interval_seconds
        self.poll_interval = poll_interval or s.poll_interval_seconds
        self.run_coordinator = run_coordinator
        self.executor = StepExecutor(get_registry(), Storage(), self.worker_id)
        self.inflight: dict[uuid.UUID, InFlight] = {}
        self.stats = WorkerStats()
        self._wake = asyncio.Event()
        self._beat_now = asyncio.Event()
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        log.info("worker_started", worker_id=self.worker_id, concurrency=self.concurrency)
        await send_heartbeat(self.worker_id, [], concurrency=self.concurrency, lease_ttl=self.lease_ttl)
        background = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._listen(), name="listen"),
        ]
        if self.run_coordinator:
            background.append(asyncio.create_task(self._coordinator_loop(), name="coordinator"))
        try:
            await self._claim_loop()
        finally:
            for t in background:
                t.cancel()
            for t in background:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await t
            await self._drain()
            with contextlib.suppress(Exception):
                await mark_worker_stopped(self.worker_id)
            log.info("worker_stopped", worker_id=self.worker_id, stats=self.stats.outcomes)

    def stop(self) -> None:
        self._stopping.set()
        self._wake.set()

    async def _drain(self, grace: float = 10.0) -> None:
        tasks = [f.task for f in self.inflight.values()]
        if not tasks:
            return
        _done, pending = await asyncio.wait(tasks, timeout=grace)
        for t in pending:
            # Abandon: the lease expires and another worker retries the step.
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

    # ------------------------------------------------------------------ loops

    async def _claim_loop(self) -> None:
        while not self._stopping.is_set():
            free = self.concurrency - len(self.inflight)
            claims: list[ClaimedStep] = []
            if free > 0:
                try:
                    claims = await claim_steps(self.worker_id, free, lease_ttl=self.lease_ttl)
                except Exception as exc:
                    log.error("claim_failed", error=str(exc))
                    await asyncio.sleep(1.0)
            for claim in claims:
                self.stats.claimed += 1
                task = asyncio.create_task(self._execute(claim), name=f"step-{claim.node_key}")
                self.inflight[claim.lease_token] = InFlight(claim, task)
            if claims and len(self.inflight) < self.concurrency:
                continue  # more work may be ready right now
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)

    async def _execute(self, claim: ClaimedStep) -> None:
        outcome = "unknown"
        try:
            outcome = await self.executor.execute(claim)
        except asyncio.CancelledError:
            entry = self.inflight.get(claim.lease_token)
            reason = entry.cancel_reason if entry else None
            outcome = f"cancelled:{reason}"
            if reason == "run_cancelled":
                with contextlib.suppress(StaleLeaseError, Exception):
                    await mark_step_cancelled(
                        claim.step_id, claim.lease_token, note="cooperative cancellation"
                    )
            # lease_lost / shutdown: write nothing; recovery owns the step now.
        except Exception as exc:
            outcome = "crashed"
            log.exception("executor_crashed", node_key=claim.node_key, error=str(exc))
        finally:
            self.inflight.pop(claim.lease_token, None)
            self.stats.outcomes[outcome] = self.stats.outcomes.get(outcome, 0) + 1
            self._wake.set()

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                tokens = list(self.inflight)
                result = await send_heartbeat(
                    self.worker_id,
                    tokens,
                    concurrency=self.concurrency,
                    lease_ttl=self.lease_ttl,
                    steps_completed=self.stats.outcomes.get("succeeded", 0),
                )
                for token in result.lost | result.cancelled:
                    entry = self.inflight.get(token)
                    if entry is None or entry.task.done():
                        continue
                    entry.cancel_reason = "run_cancelled" if token in result.cancelled else "lease_lost"
                    log.warning("cancelling_step", node_key=entry.claim.node_key, reason=entry.cancel_reason)
                    entry.task.cancel()
            except Exception as exc:
                log.error("heartbeat_failed", error=str(exc))
            self._beat_now.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._beat_now.wait(), timeout=self.heartbeat_interval)

    async def _coordinator_loop(self) -> None:
        interval = get_settings().coordinator_interval_seconds
        while True:
            try:
                await coordinator_tick()
            except Exception as exc:
                log.error("coordinator_failed", error=str(exc))
            await asyncio.sleep(interval)

    async def _listen(self) -> None:
        """Redis wake-ups. Optional: if Redis is down the loops simply poll."""
        while True:
            pubsub = None
            try:
                pubsub = get_notifier().client.pubsub()
                await pubsub.subscribe(WORK_CHANNEL, CANCEL_CHANNEL)
                async for message in pubsub.listen():
                    if message.get("type") != "message":
                        continue
                    if message.get("channel") == CANCEL_CHANNEL:
                        self._beat_now.set()
                    else:
                        self._wake.set()
                    _ = json.loads(message.get("data") or "{}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("redis_listen_failed", error=str(exc))
                await asyncio.sleep(5.0)
            finally:
                if pubsub is not None:
                    with contextlib.suppress(Exception):
                        await pubsub.aclose()  # type: ignore[no-untyped-call]
