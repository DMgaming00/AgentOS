"""Benchmark harness: isolated database, real worker processes, metrics from persisted timestamps.

Every number is derived from rows written by the engine (attempt start/finish
times, ready_at, queue_wait_ms, run created/completed) plus psutil samples of the
worker processes. Nothing is estimated or synthesized.
"""

from __future__ import annotations

import asyncio
import os
import platform
import statistics
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PG = os.environ.get("AGENTOS_BENCH_PG", "localhost:55432")
DB = "agentos_bench"
DB_URL = f"postgresql+asyncpg://agentos:agentos@{PG}/{DB}"

BENCH_ENV = {
    "AGENTOS_ENVIRONMENT": "test",
    "AGENTOS_DATABASE_URL": DB_URL,
    "AGENTOS_REDIS_URL": os.environ.get("AGENTOS_BENCH_REDIS", "redis://localhost:56379/14"),
    "AGENTOS_ARTIFACT_DIR": str(ROOT / "var" / "bench-artifacts"),
    "AGENTOS_LOG_LEVEL": "WARNING",
    "AGENTOS_LOG_JSON": "false",
    "AGENTOS_POLL_INTERVAL_SECONDS": "0.5",
}
os.environ.update(BENCH_ENV)

import asyncpg  # noqa: E402
import psutil  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from agentos_core.db.models import StepAttempt, WorkflowRun, WorkflowStep  # noqa: E402
from agentos_core.db.session import get_sessionmaker  # noqa: E402
from agentos_core.testing import Seeded, seed_demo, start_run  # noqa: E402

TABLES = (
    "events, model_usage, artifacts, approval_requests, tool_executions, step_attempts, workflow_steps, "
    "workflow_runs, workflow_definitions, agent_definitions, document_chunks, documents, idempotency_records, "
    "workers, workspace_members, workspaces, users"
)


def machine_info() -> dict[str, Any]:
    info = {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
        "memory_gb": round(psutil.virtual_memory().total / 2**30, 1),
    }
    try:
        info["cpu"] = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        info["cpu"] = platform.processor()
    try:
        pg = subprocess.run(
            ["docker", "compose", "exec", "-T", "postgres", "postgres", "--version"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        ).stdout.strip()
        info["postgres"] = pg or "unknown"
    except OSError:
        info["postgres"] = "unknown"
    return info


async def create_database() -> None:
    conn = await asyncpg.connect(f"postgresql://agentos:agentos@{PG}/postgres")
    try:
        await conn.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{DB}'")
        await conn.execute(f'DROP DATABASE IF EXISTS "{DB}"')
        await conn.execute(f'CREATE DATABASE "{DB}"')
    finally:
        await conn.close()
    await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "alembic", "-c", str(ROOT / "apps/api/alembic.ini"), "upgrade", "head"],
        check=True,
        capture_output=True,
        env={**os.environ, **BENCH_ENV},
    )


async def reset() -> Seeded:
    async with get_sessionmaker()() as s:
        await s.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
        await s.commit()
    return await seed_demo()


@dataclass
class WorkerPool:
    count: int
    concurrency: int
    env: dict[str, str] = field(default_factory=dict)
    supervise: bool = False
    procs: list[subprocess.Popen[bytes]] = field(default_factory=list)
    samples: list[tuple[float, float]] = field(default_factory=list)  # (cpu %, rss MB) summed over workers
    restarts: int = 0
    _ps: dict[int, psutil.Process] = field(default_factory=dict)
    lock_samples: list[tuple[int, int]] = field(default_factory=list)  # (waiting on lock, active)

    def _spawn(self, i: int) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentos_worker.main",
                "--id",
                f"bench-{i}-{uuid.uuid4().hex[:4]}",
                "--concurrency",
                str(self.concurrency),
            ],
            env={**os.environ, **BENCH_ENV, **self.env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def start(self) -> None:
        self.procs = [self._spawn(i) for i in range(self.count)]

    def tick(self) -> None:
        cpu = rss = 0.0
        for i, p in enumerate(self.procs):
            if p.poll() is not None:
                if self.supervise:
                    self.procs[i] = self._spawn(i)
                    self.restarts += 1
                continue
            try:
                pp = self._ps.get(p.pid)
                if pp is None:  # first call primes the counter (returns 0)
                    pp = self._ps[p.pid] = psutil.Process(p.pid)
                    pp.cpu_percent(interval=None)
                cpu += pp.cpu_percent(interval=None)
                rss += pp.memory_info().rss / 2**20
            except psutil.Error:
                pass
        self.samples.append((cpu, rss))

    def stop(self) -> None:
        for p in self.procs:
            if p.poll() is None:
                p.terminate()
        for p in self.procs:
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()

    def resource_summary(self) -> dict[str, float]:
        if not self.samples:
            return {"lock_wait_share": 0.0, "cpu_pct_mean": 0.0, "cpu_pct_max": 0.0, "rss_mb_max": 0.0}
        cpus = [c for c, _ in self.samples[1:]] or [0.0]
        waiting = sum(w for w, _ in self.lock_samples)
        active = sum(a for _, a in self.lock_samples)
        return {
            "lock_wait_share": round(waiting / active, 3) if active else 0.0,
            "cpu_pct_mean": round(statistics.fmean(cpus), 1),
            "cpu_pct_max": round(max(cpus), 1),
            "rss_mb_max": round(max(r for _, r in self.samples), 1),
        }


@contextmanager
def workers(count: int, concurrency: int, **kw: Any):  # type: ignore[no-untyped-def]
    pool = WorkerPool(count, concurrency, **kw)
    pool.start()
    try:
        yield pool
    finally:
        pool.stop()


async def wait_runs(run_ids: list[uuid.UUID], pool: WorkerPool, timeout: float = 600) -> float:
    """Wait until every run is terminal; returns wall seconds. Samples worker resources meanwhile."""
    start = time.perf_counter()
    while True:
        async with get_sessionmaker()() as s:
            remaining = (
                (
                    await s.execute(
                        select(WorkflowRun.id).where(
                            WorkflowRun.id.in_(run_ids),
                            WorkflowRun.status.not_in(["succeeded", "failed", "cancelled"]),
                        )
                    )
                )
                .scalars()
                .all()
            )
        pool.tick()
        await sample_locks(pool)
        if not remaining:
            return time.perf_counter() - start
        if time.perf_counter() - start > timeout:
            raise TimeoutError(f"{len(remaining)} runs unfinished after {timeout}s")
        await asyncio.sleep(0.25)


async def sample_locks(pool: WorkerPool) -> None:
    """Sample backend states: how many AgentOS connections are waiting on a lock right now."""
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(
                text(
                    "SELECT count(*) FILTER (WHERE wait_event_type = 'Lock'), count(*) FILTER (WHERE state = 'active') "
                    "FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
                )
            )
        ).one()
    pool.lock_samples.append((int(row[0]), int(row[1])))


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


async def run_metrics(run_ids: list[uuid.UUID]) -> dict[str, Any]:
    async with get_sessionmaker()() as s:
        runs = (await s.execute(select(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))).scalars().all()
        attempts = (
            (await s.execute(select(StepAttempt).where(StepAttempt.run_id.in_(run_ids)))).scalars().all()
        )
        steps = (
            (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id.in_(run_ids)))).scalars().all()
        )
    qwait = [float(a.queue_wait_ms) for a in attempts if a.queue_wait_ms is not None]
    durations = [(r.completed_at - r.created_at).total_seconds() for r in runs if r.completed_at]
    first = min(r.created_at for r in runs)
    last = max(r.completed_at for r in runs if r.completed_at)
    return {
        "runs": len(runs),
        "succeeded": sum(r.status == "succeeded" for r in runs),
        "failed": sum(r.status == "failed" for r in runs),
        "steps": len(steps),
        "attempts": len(attempts),
        "makespan_s": (last - first).total_seconds(),
        "run_duration_p50_s": pct(durations, 0.5),
        "run_duration_p95_s": pct(durations, 0.95),
        "queue_wait_p50_ms": pct(qwait, 0.5),
        "queue_wait_p95_ms": pct(qwait, 0.95),
        "attempts_list": attempts,
        "steps_list": steps,
    }


__all__ = [
    "DB_URL",
    "Seeded",
    "WorkerPool",
    "create_database",
    "machine_info",
    "pct",
    "reset",
    "run_metrics",
    "start_run",
    "wait_runs",
    "workers",
]
