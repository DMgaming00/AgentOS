"""Fault injection against the v2 constructs with real worker *processes*.

Delivery semantics under test: at-least-once for retryable steps (a step whose worker died is run again),
never for completed steps, with fenced writes so a stale worker cannot overwrite a newer result.
"""

import asyncio

import pytest
from sqlalchemy import select

from agentos_core.db.models import ApprovalRequest, StepAttempt, WorkflowRun
from agentos_core.domain.states import ApprovalStatus
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import (
    get_steps,
    pending_approvals,
    save_workflow,
    spawn_worker,
    start_run,
    stop_workers,
    wait_exit,
    wait_for,
    wait_run_status,
)

pytestmark = pytest.mark.integration


def calc(key, a=1, b=1, **kw):
    return {
        "key": key,
        "type": "tool_task",
        "tool": "calculator",
        "arguments": {"operation": "add", "values": [a, b]},
        **kw,
    }


async def attempts_of(step_id):
    async with unit_of_work() as s:
        rows = (
            (
                await s.execute(
                    select(StepAttempt)
                    .where(StepAttempt.step_id == step_id)
                    .order_by(StepAttempt.attempt_number)
                )
            )
            .scalars()
            .all()
        )
    return [(a.worker_id, a.status, a.error_type) for a in rows]


async def test_crash_inside_a_child_run_recovers_and_the_waiting_parent_completes(seeded):
    await save_workflow(
        seeded, "child-crashy", {"nodes": [calc("risky", 2, 3, inject={"crash_worker_on_attempt": [1]})]}
    )
    parent = {
        "nodes": [
            {"key": "go", "type": "sub_workflow", "workflow": "child-crashy"},
            {"key": "after", "type": "aggregation", "depends_on": ["go"]},
        ]
    }
    run_id = await start_run(seeded, parent)
    w1, w2 = spawn_worker("child-crasher"), None
    try:
        assert await wait_exit(w1) == 137
        steps = await get_steps(run_id)
        assert steps["go"].status == "waiting_child"  # the wait is durable state, not a process
        assert (await wait_run_status(run_id, {"running"}, 5)).status == "running"
        w2 = spawn_worker("child-rescuer")
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=40)
        assert run.status == "succeeded", run.error
    finally:
        stop_workers(w1, *(p for p in [w2] if p))
    steps = await get_steps(run_id)
    async with unit_of_work() as s:
        child = (await s.execute(select(WorkflowRun).where(WorkflowRun.parent_run_id == run_id))).scalar_one()
    risky = (await get_steps(child.id))["risky"]
    assert await attempts_of(risky.id) == [
        ("child-crasher", "lease_expired", "worker_lost"),
        ("child-rescuer", "succeeded", None),
    ]
    assert steps["go"].attempt_count == 0  # engine-handled: never leased, so nothing to lose or repeat
    assert steps["go"].output["output"]["risky"]["result"] == 5.0


async def test_crash_after_fan_out_children_finish_does_not_rerun_them(seeded):
    wf = {
        "nodes": [
            {
                "key": "fan",
                "type": "fan_out",
                "over": "input.xs",
                "template": {
                    "type": "tool_task",
                    "tool": "calculator",
                    "arguments": {"operation": "add", "values": [{"$ref": "item"}, 1]},
                },
            },
            {
                "key": "after",
                "type": "aggregation",
                "depends_on": ["fan"],
                "inject": {"crash_worker_on_attempt": [1]},
            },
        ]
    }
    run_id = await start_run(seeded, wf, run_input={"xs": [1, 2, 3]})
    w1, w2 = spawn_worker("fan-crasher"), None
    try:
        assert await wait_exit(w1) == 137
        steps = await get_steps(run_id)
        assert all(steps[f"fan[{i}]"].status == "succeeded" for i in range(3))
        assert steps["after"].status == "running"
        w2 = spawn_worker("fan-rescuer")
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=40)
        assert run.status == "succeeded", run.error
    finally:
        stop_workers(w1, *(p for p in [w2] if p))
    steps = await get_steps(run_id)
    assert [steps[f"fan[{i}]"].attempt_count for i in range(3)] == [1, 1, 1]  # completed work not repeated
    assert [r["result"] for r in steps["fan"].output["results"]] == [2.0, 3.0, 4.0]
    assert await attempts_of(steps["after"].id) == [
        ("fan-crasher", "lease_expired", "worker_lost"),
        ("fan-rescuer", "succeeded", None),
    ]


async def test_killing_a_worker_mid_fan_out_recovers_every_child_exactly_once_in_the_result(seeded):
    wf = {
        "nodes": [
            {
                "key": "fan",
                "type": "fan_out",
                "over": "input.xs",
                "template": {
                    "type": "tool_task",
                    "tool": "calculator",
                    "arguments": {"operation": "add", "values": [{"$ref": "item"}, 100]},
                    "inject": {"sleep_seconds": 4},
                },
            }
        ]
    }
    run_id = await start_run(seeded, wf, run_input={"xs": [1, 2, 3, 4]})
    w1, w2 = spawn_worker("victim"), None
    try:

        async def children_running():
            steps = await get_steps(run_id)
            return sum(1 for k, s in steps.items() if k.startswith("fan[") and s.status == "running") == 4

        await wait_for(children_running, timeout=30)
        w1.kill()  # SIGKILL: no cleanup, leases are simply abandoned
        w1.wait(timeout=10)
        w2 = spawn_worker("survivor")
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=60)
        assert run.status == "succeeded", run.error
    finally:
        stop_workers(w1, *(p for p in [w2] if p))
    steps = await get_steps(run_id)
    assert sorted(k for k in steps if k.startswith("fan[")) == [
        f"fan[{i}]" for i in range(4)
    ]  # no duplicates
    assert [r["result"] for r in steps["fan"].output["results"]] == [101.0, 102.0, 103.0, 104.0]
    for i in range(4):
        assert [a[1] for a in await attempts_of(steps[f"fan[{i}]"].id)] == ["lease_expired", "succeeded"]


async def test_pending_approval_survives_a_killed_worker_and_the_run_converges(seeded):
    wf = {
        "nodes": [
            calc("prep"),
            {"key": "gate", "type": "approval_gate", "depends_on": ["prep"], "action": "ship it"},
            {"key": "final", "type": "aggregation", "depends_on": ["gate"]},
        ]
    }
    run_id = await start_run(seeded, wf)
    w1, w2 = spawn_worker("before-restart"), None
    try:

        async def first_pending():
            items = await pending_approvals(run_id)
            return items[0] if items else None

        approval = await wait_for(first_pending, timeout=30)
        w1.kill()
        w1.wait(timeout=10)
        await asyncio.sleep(0.5)
        async with unit_of_work() as s:
            row = await s.get(ApprovalRequest, approval.id)
        assert row.status == "pending"  # durable across the kill
        assert (
            await wait_run_status(run_id, {"waiting_approval", "running"}, 5)
        ).status == "waiting_approval"
        w2 = spawn_worker("after-restart")
        async with unit_of_work() as s:
            await core.resolve_approval(
                s,
                approval.id,
                decision=ApprovalStatus.APPROVED,
                user_id=seeded.users["admin"].id,
                reason="ok",
            )
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=40)
        assert run.status == "succeeded", run.error
    finally:
        stop_workers(w1, *(p for p in [w2] if p))
    async with unit_of_work() as s:
        count = len(
            (await s.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == run_id))).scalars().all()
        )
    assert count == 1  # the restart did not create a second approval
