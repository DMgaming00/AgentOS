"""Crash recovery with real worker *processes* (the crashing process exits with status 137)."""

import asyncio
import os
import subprocess
import sys

import pytest
from sqlalchemy import func, select

from agentos_core.db.models import Artifact, StepAttempt, ToolExecution
from agentos_core.demo import approval_workflow, failure_recovery_workflow
from agentos_core.domain.states import ApprovalStatus
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import get_steps, pending_approvals, start_run, wait_for, wait_run_status

pytestmark = pytest.mark.integration


def spawn_worker(worker_id: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "agentos_worker.main", "--id", worker_id, "--concurrency", "4"],
        env={**os.environ, "AGENTOS_LOG_LEVEL": "WARNING"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )


async def wait_exit(proc: subprocess.Popen, timeout: float = 30) -> int:
    async def done():
        return proc.poll() is not None

    await wait_for(done, timeout)
    return proc.returncode


def stop(*procs: subprocess.Popen) -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


async def approve_next(run_id, user_id, *, exclude=()):
    async def find():
        items = [a for a in await pending_approvals(run_id) if a.id not in exclude]
        return items[0] if items else None

    ap = await wait_for(find, timeout=30)
    async with unit_of_work() as s:
        await core.resolve_approval(s, ap.id, decision=ApprovalStatus.APPROVED, user_id=user_id, reason="ok")
    return ap


async def test_worker_crash_is_recovered_without_rerunning_completed_steps(seeded):
    run_id = await start_run(seeded, failure_recovery_workflow(), name="failure-recovery")
    w1 = spawn_worker("crashy-worker")
    w2 = None
    try:
        assert await wait_exit(w1) == 137  # killed itself mid-step via fault injection
        steps = await get_steps(run_id)
        assert steps["fetch_input"].status == "succeeded"
        assert steps["process_b"].status == "succeeded"
        assert steps["process_a"].status == "running" and steps["process_a"].lease_owner == "crashy-worker"

        w2 = spawn_worker("rescue-worker")
        await approve_next(run_id, seeded.users["admin"].id)
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=40)
        assert run.status == "succeeded", run.error
    finally:
        stop(w1, *(p for p in [w2] if p))

    steps = await get_steps(run_id)
    assert steps["fetch_input"].attempt_count == 1
    assert steps["process_b"].attempt_count == 1  # completed work was not re-executed
    async with unit_of_work() as s:
        attempts = (
            (
                await s.execute(
                    select(StepAttempt)
                    .where(StepAttempt.step_id == steps["process_a"].id)
                    .order_by(StepAttempt.attempt_number)
                )
            )
            .scalars()
            .all()
        )
        artifacts = (
            await s.execute(select(func.count()).select_from(Artifact).where(Artifact.run_id == run_id))
        ).scalar_one()
    assert [(a.worker_id, a.status, a.error_type) for a in attempts] == [
        ("crashy-worker", "lease_expired", "worker_lost"),
        ("rescue-worker", "succeeded", None),
    ]
    assert artifacts == 1
    assert steps["aggregate"].output["inputs"]["process_a"]["processed"]["shard"] == "A"


async def test_crash_after_side_effect_does_not_duplicate_artifact(seeded):
    """Demo C: reject -> resubmit -> approve -> worker dies right after writing -> retry reuses the artifact."""
    wf = approval_workflow()
    run_id = await start_run(seeded, wf, name="approval-crash")
    w1 = spawn_worker("writer-worker")
    w2 = None
    try:

        async def first_pending():
            items = await pending_approvals(run_id)
            return items[0] if items else None

        first = await wait_for(first_pending, timeout=30)
        async with unit_of_work() as s:
            await core.resolve_approval(
                s,
                first.id,
                decision=ApprovalStatus.REJECTED,
                user_id=seeded.users["admin"].id,
                reason="needs a revision",
            )
        await approve_next(run_id, seeded.users["admin"].id, exclude={first.id})
        assert await wait_exit(w1) == 137  # crashed after the artifact write, before recording the step
        w2 = spawn_worker("retry-worker")
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=40)
        assert run.status == "succeeded", run.error
    finally:
        stop(w1, *(p for p in [w2] if p))

    async with unit_of_work() as s:
        artifacts = (await s.execute(select(Artifact).where(Artifact.run_id == run_id))).scalars().all()
        writes = (
            (
                await s.execute(
                    select(ToolExecution.status)
                    .where(
                        ToolExecution.run_id == run_id,
                        ToolExecution.tool_name == "artifact_writer",
                        ToolExecution.status != "denied",
                    )
                    .order_by(ToolExecution.created_at)
                )
            )
            .scalars()
            .all()
        )
    assert len(artifacts) == 1 and artifacts[0].filename == "approved-note.md"
    assert writes == ["rejected", "succeeded"]  # exactly one executed write
    out = (await get_steps(run_id))["draft_note"].output
    assert out["status"] == "written" and out["artifact"]["idempotent_replay"] is True
    await asyncio.sleep(0)
