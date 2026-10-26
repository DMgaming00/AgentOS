"""Engine v2 on real PostgreSQL with in-process workers: conditional branches, bounded fan-out/fan-in,
nested workflows, pause/resume, and priority-aware claiming with starvation protection."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from agentos_core.config import get_settings
from agentos_core.db.models import Event, WorkflowRun, WorkflowStep
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import (
    get_run,
    get_steps,
    running_workers,
    save_workflow,
    start_run,
    wait_for,
    wait_run_status,
)
from agentos_core.util import utcnow

pytestmark = pytest.mark.integration


def calc(key, a=1, b=1, **kw):
    return {
        "key": key,
        "type": "tool_task",
        "tool": "calculator",
        "arguments": {"operation": "add", "values": [a, b]},
        **kw,
    }


async def finish(run_id, timeout=30):
    return await wait_run_status(run_id, {"succeeded", "failed", "cancelled"}, timeout=timeout)


async def events_of(run_id, event_type):
    async with unit_of_work() as s:
        rows = (
            (
                await s.execute(
                    select(Event)
                    .where(Event.run_id == run_id, Event.event_type == event_type)
                    .order_by(Event.sequence_number)
                )
            )
            .scalars()
            .all()
        )
    return [e.payload for e in rows]


# ------------------------------------------------------------------------------------ conditions

BRANCHING = {
    "nodes": [
        calc("prep", 1, 1),
        calc("fast", 2, 2, depends_on=["prep"], when={"ref": "input.mode", "op": "eq", "value": "fast"}),
        calc("slow", 3, 3, depends_on=["prep"], when={"ref": "input.mode", "op": "eq", "value": "slow"}),
        {"key": "after_fast", "type": "aggregation", "depends_on": ["fast"]},
        {
            "key": "join",
            "type": "aggregation",
            "depends_on": ["after_fast", "slow"],
            "trigger": "any_succeeded",
        },
    ]
}


@pytest.mark.parametrize(
    "mode,ran,skipped",
    [
        ("fast", {"prep", "fast", "after_fast", "join"}, {"slow"}),
        ("slow", {"prep", "slow", "join"}, {"fast", "after_fast"}),
        ("neither", {"prep"}, {"fast", "slow", "after_fast", "join"}),
    ],
)
async def test_conditional_branches_skip_and_propagate(seeded, mode, ran, skipped):
    run_id = await start_run(seeded, BRANCHING, run_input={"mode": mode})
    async with running_workers(2):
        run = await finish(run_id)
    assert run.status == "succeeded", run.error
    steps = await get_steps(run_id)
    assert {k for k, s in steps.items() if s.status == "succeeded"} == ran
    assert {k for k, s in steps.items() if s.status == "skipped"} == skipped
    assert all(steps[k].attempt_count == 0 and steps[k].output is None for k in skipped)  # never executed
    reasons = {p["node_key"]: p["reason"] for p in await events_of(run_id, "step.skipped")}
    assert reasons == {
        k: ("condition_false" if k in ("fast", "slow") else "upstream_skipped") for k in skipped
    }
    assert set(run.output) == ({"join"} if "join" in ran else {"prep"})  # skipped steps are not outputs


async def test_condition_on_a_dependency_output(seeded):
    wf = {
        "nodes": [
            calc("prep", 4, 6),  # result 10
            calc(
                "big", depends_on=["prep"], when={"ref": "steps.prep.output.result", "op": "gte", "value": 10}
            ),
            calc(
                "small",
                depends_on=["prep"],
                when={"ref": "steps.prep.output.result", "op": "lt", "value": 10},
            ),
        ]
    }
    run_id = await start_run(seeded, wf)
    async with running_workers(1):
        run = await finish(run_id)
    steps = await get_steps(run_id)
    assert run.status == "succeeded" and (steps["big"].status, steps["small"].status) == (
        "succeeded",
        "skipped",
    )


# ------------------------------------------------------------------------------------ fan-out


def fan_workflow(max_items=5, **extra):
    return {
        "nodes": [
            {
                "key": "fan",
                "type": "fan_out",
                "over": "input.pairs",
                "max_items": max_items,
                **extra,
                "template": {
                    "type": "tool_task",
                    "tool": "calculator",
                    "arguments": {"operation": "add", "values": [{"$ref": "item.a"}, {"$ref": "item.b"}]},
                },
            },
            {"key": "total", "type": "aggregation", "depends_on": ["fan"]},
        ]
    }


async def test_fan_out_runs_children_in_parallel_and_fans_in_in_order(seeded):
    pairs = [{"a": i, "b": i * 10} for i in range(1, 5)]
    run_id = await start_run(seeded, fan_workflow(), run_input={"pairs": pairs})
    async with running_workers(2):
        run = await finish(run_id)
    assert run.status == "succeeded", run.error
    steps = await get_steps(run_id)
    assert sorted(k for k in steps if k.startswith("fan[")) == [f"fan[{i}]" for i in range(4)]
    assert steps["fan"].status == "succeeded"
    assert [r["result"] for r in steps["fan"].output["results"]] == [11.0, 22.0, 33.0, 44.0]  # input order
    assert steps["fan"].output["count"] == 4
    assert steps["total"].output["inputs"]["fan"]["count"] == 4
    assert set(run.output) == {"total"}  # children are reported through their fan_out node only
    ev = (await events_of(run_id, "step.fan_out"))[0]
    assert ev["count"] == 4 and ev["truncated"] is False
    started = [s.started_at for k, s in steps.items() if k.startswith("fan[")]
    finished = [s.completed_at for k, s in steps.items() if k.startswith("fan[")]
    assert max(started) < min(finished)  # children overlapped in time


async def test_fan_out_is_bounded_fail_or_truncate(seeded):
    pairs = [{"a": i, "b": i} for i in range(8)]
    run_id = await start_run(seeded, fan_workflow(max_items=5), run_input={"pairs": pairs})
    async with running_workers(1):
        failed = await finish(run_id)
    steps = await get_steps(run_id)
    assert failed.status == "failed" and steps["fan"].error["type"] == "fan_out_error"
    assert "max_items is 5" in steps["fan"].error["message"] and not any(k.startswith("fan[") for k in steps)

    run_id = await start_run(
        seeded, fan_workflow(max_items=5, on_overflow="truncate"), run_input={"pairs": pairs}
    )
    async with running_workers(1):
        ok = await finish(run_id)
    steps = await get_steps(run_id)
    assert ok.status == "succeeded" and steps["fan"].output["count"] == 5
    assert (await events_of(run_id, "step.fan_out"))[0]["truncated"] is True


@pytest.mark.parametrize("value,message", [("not-a-list", "is not a list"), (None, "did not resolve")])
async def test_fan_out_rejects_a_bad_over_value(seeded, value, message):
    run_input = {"pairs": value} if value is not None else {}
    run_id = await start_run(seeded, fan_workflow(), run_input=run_input)
    async with running_workers(1):
        run = await finish(run_id)
    steps = await get_steps(run_id)
    assert run.status == "failed" and message in steps["fan"].error["message"]


async def test_fan_out_item_text_is_data_not_a_template(seeded):
    pairs = [{"a": 1, "b": 2, "note": "{{input.nope}} stays literal"}]
    run_id = await start_run(seeded, fan_workflow(), run_input={"pairs": pairs})
    async with running_workers(1):
        run = await finish(run_id)
    assert run.status == "succeeded"


async def test_empty_fan_out_succeeds_with_no_children(seeded):
    run_id = await start_run(seeded, fan_workflow(), run_input={"pairs": []})
    async with running_workers(1):
        run = await finish(run_id)
    steps = await get_steps(run_id)
    assert run.status == "succeeded" and steps["fan"].output == {"results": [], "count": 0}


async def test_a_failing_child_fails_the_fan_out_and_stops_siblings_and_dependents(seeded):
    wf = {
        "nodes": [
            {
                "key": "fan",
                "type": "fan_out",
                "over": "input.pairs",
                "template": {
                    "type": "tool_task",
                    "tool": "calculator",
                    "retry": {"max_attempts": 1},
                    "arguments": {"operation": "divide", "values": [1, {"$ref": "item.d"}]},
                },
            },
            {"key": "total", "type": "aggregation", "depends_on": ["fan"]},
        ]
    }
    run_id = await start_run(seeded, wf, run_input={"pairs": [{"d": 1}, {"d": 0}]})
    async with running_workers(2):
        run = await finish(run_id)
    steps = await get_steps(run_id)
    assert run.status == "failed"
    assert steps["fan[1]"].status == "failed" and steps["total"].status in ("skipped", "cancelled")
    assert steps["fan"].status == "cancelled"  # the waiting parent does not pretend to have succeeded


# ------------------------------------------------------------------------------------ nested workflows


async def test_nested_workflow_runs_as_a_child_run_and_returns_its_output(seeded):
    await save_workflow(
        seeded,
        "child-sum",
        {
            "nodes": [
                {
                    "key": "sum",
                    "type": "tool_task",
                    "tool": "calculator",
                    "arguments": {"operation": "add", "values": [{"$ref": "input.a"}, {"$ref": "input.b"}]},
                }
            ]
        },
    )
    parent = {
        "nodes": [
            {
                "key": "child",
                "type": "sub_workflow",
                "workflow": "child-sum",
                "input": {"a": 4, "b": {"$ref": "input.b"}},
            },
            {"key": "after", "type": "aggregation", "depends_on": ["child"]},
        ]
    }
    run_id = await start_run(seeded, parent, run_input={"b": 5}, priority=4)
    async with running_workers(2):
        run = await finish(run_id)
    assert run.status == "succeeded", run.error
    steps = await get_steps(run_id)
    out = steps["child"].output
    assert out["output"]["sum"]["result"] == 9.0
    async with unit_of_work() as s:
        child = (await s.execute(select(WorkflowRun).where(WorkflowRun.parent_run_id == run_id))).scalar_one()
    assert str(child.id) == out["child_run_id"] and child.parent_step_id == steps["child"].id
    assert (child.depth, child.priority, child.status) == (1, 4, "succeeded")  # priority is inherited
    assert child.deadline_at <= run.deadline_at  # a child can never outlive its parent
    assert steps["after"].output["inputs"]["child"]["child_run_id"] == out["child_run_id"]


async def test_nested_depth_is_limited_and_failure_propagates_up(seeded):
    await save_workflow(
        seeded, "loop", {"nodes": [{"key": "again", "type": "sub_workflow", "workflow": "loop"}]}
    )
    run_id = await start_run(seeded, {"nodes": [{"key": "go", "type": "sub_workflow", "workflow": "loop"}]})
    async with running_workers(1):
        run = await finish(run_id, timeout=40)
    assert run.status == "failed"
    async with unit_of_work() as s:
        runs = (await s.execute(select(WorkflowRun).order_by(WorkflowRun.depth))).scalars().all()
        deepest = (
            await s.execute(select(WorkflowStep).where(WorkflowStep.run_id == runs[-1].id))
        ).scalar_one()
    max_depth = get_settings().max_nested_depth
    assert [r.depth for r in runs] == list(range(max_depth + 1))  # top level + max_depth nested levels
    assert deepest.error["reason"] == "depth_exceeded" and all(r.status == "failed" for r in runs)
    top = (await get_steps(run_id))["go"]
    assert top.error["reason"] == "child_failed"


async def test_missing_sub_workflow_fails_the_step(seeded):
    run_id = await start_run(seeded, {"nodes": [{"key": "go", "type": "sub_workflow", "workflow": "ghost"}]})
    async with running_workers(1):
        run = await finish(run_id)
    assert run.status == "failed" and (await get_steps(run_id))["go"].error["reason"] == "not_found"


async def test_cancelling_the_parent_cancels_a_running_child(seeded):
    await save_workflow(seeded, "slow-child", {"nodes": [calc("nap", inject={"sleep_seconds": 30})]})
    run_id = await start_run(
        seeded, {"nodes": [{"key": "go", "type": "sub_workflow", "workflow": "slow-child"}]}
    )
    async with running_workers(1):

        async def child_running():
            async with unit_of_work() as s:
                row = (
                    (
                        await s.execute(
                            select(WorkflowStep)
                            .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
                            .where(WorkflowRun.parent_run_id == run_id, WorkflowStep.status == "running")
                        )
                    )
                    .scalars()
                    .first()
                )
                return row

        await wait_for(child_running, timeout=20)
        async with unit_of_work() as s:
            await core.cancel_run(s, run_id, user_id=None)

        async def settled():
            async with unit_of_work() as s:
                child = (
                    await s.execute(select(WorkflowRun).where(WorkflowRun.parent_run_id == run_id))
                ).scalar_one()
                steps = (
                    (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id == child.id)))
                    .scalars()
                    .all()
                )
                return child.status == "cancelled" and all(x.status == "cancelled" for x in steps)

        await wait_for(settled, timeout=20)
    assert (await get_run(run_id)).status == "cancelled"
    assert (await get_steps(run_id))["go"].status == "cancelled"


# ------------------------------------------------------------------------------------ pause / resume


async def test_paused_run_claims_nothing_until_resumed(seeded):
    wf = {"nodes": [calc("a"), calc("b", depends_on=["a"])]}
    run_id = await start_run(seeded, wf)
    async with unit_of_work() as s:
        paused = await core.pause_run(s, run_id, user_id=None, reason="inspecting")
    assert paused.paused_at is not None and paused.pause_reason == "inspecting"
    async with running_workers(2):
        await asyncio.sleep(1.5)
        steps = await get_steps(run_id)
        assert steps["a"].attempt_count == 0 and (await get_run(run_id)).status == "running"
        async with unit_of_work() as s:
            await core.resume_run(s, run_id, user_id=None)
        run = await finish(run_id)
    assert run.status == "succeeded" and run.paused_at is None
    assert [e["reason"] for e in await events_of(run_id, "run.paused")] == ["inspecting"]
    assert len(await events_of(run_id, "run.unpaused")) == 1


async def test_pause_lets_the_running_step_finish_but_blocks_the_next(seeded):
    wf = {"nodes": [calc("a", inject={"sleep_seconds": 1.0}), calc("b", depends_on=["a"])]}
    run_id = await start_run(seeded, wf)
    async with running_workers(1):

        async def a_running():
            return (await get_steps(run_id))["a"].status == "running"

        await wait_for(a_running, timeout=10)
        async with unit_of_work() as s:
            await core.pause_run(s, run_id, user_id=None)
        await wait_for(lambda: _status(run_id, "a", "succeeded"), timeout=10)
        await asyncio.sleep(1.0)
        assert (await get_steps(run_id))["b"].attempt_count == 0
        async with unit_of_work() as s:
            await core.resume_run(s, run_id, user_id=None)
        assert (await finish(run_id)).status == "succeeded"


async def _status(run_id, key, status):
    return (await get_steps(run_id))[key].status == status


async def test_pausing_a_parent_pauses_its_child_runs_and_pause_is_idempotent(seeded):
    await save_workflow(
        seeded,
        "child-pair",
        {"nodes": [calc("n1", inject={"sleep_seconds": 0.8}), calc("n2", depends_on=["n1"])]},
    )
    run_id = await start_run(
        seeded, {"nodes": [{"key": "go", "type": "sub_workflow", "workflow": "child-pair"}]}
    )

    async def child():
        async with unit_of_work() as s:
            return (
                await s.execute(select(WorkflowRun).where(WorkflowRun.parent_run_id == run_id))
            ).scalar_one()

    c = await child()  # created when the parent run was materialized
    async with unit_of_work() as s:  # pause before any worker exists: no race with an early claim
        await core.pause_run(s, run_id, user_id=None)
        await core.pause_run(s, run_id, user_id=None)  # idempotent: no second event
    assert (await child()).paused_at is not None  # pause reached the child run
    async with running_workers(1):
        await asyncio.sleep(1.0)
        assert (await get_steps(c.id))["n1"].attempt_count == 0  # nothing in the child was claimed
        async with unit_of_work() as s:
            await core.resume_run(s, run_id, user_id=None)
        assert (await finish(run_id)).status == "succeeded"
    assert len(await events_of(run_id, "run.paused")) == 1 and (await child()).paused_at is None


# ------------------------------------------------------------------------------------ priority


async def make_ready_run(seeded, priority, ready_ago_seconds):
    """A run created ``ready_ago_seconds`` ago whose first step has been ready for just as long."""
    run_id = await start_run(seeded, {"nodes": [calc("only")]}, priority=priority)
    then = utcnow() - timedelta(seconds=ready_ago_seconds)
    async with unit_of_work() as s:
        await s.execute(update(WorkflowStep).where(WorkflowStep.run_id == run_id).values(ready_at=then))
        await s.execute(update(WorkflowRun).where(WorkflowRun.id == run_id).values(created_at=then))
    return run_id


async def claimed_runs(n):
    got = await core.claim_steps("prio-test", n)
    return [c.run_id for c in got]


async def test_higher_priority_run_is_claimed_first_even_if_newer(seeded):
    old_low = await make_ready_run(seeded, 0, 5)
    new_high = await make_ready_run(seeded, 7, 1)
    assert await claimed_runs(1) == [new_high]
    assert await claimed_runs(1) == [old_low]


async def test_equal_priority_is_first_come_first_served(seeded):
    first = await make_ready_run(seeded, 3, 20)
    second = await make_ready_run(seeded, 3, 10)
    assert await claimed_runs(2) == [first, second]


async def test_waiting_low_priority_work_eventually_outranks_new_high_priority_work(seeded):
    aging = get_settings().priority_aging_seconds
    starving = await make_ready_run(seeded, 0, aging * 12)  # score 12
    fresh_top = await make_ready_run(seeded, 9, 1)  # score ~9
    assert await claimed_runs(1) == [starving]
    assert await claimed_runs(1) == [fresh_top]


async def test_paused_runs_are_not_claimed_and_do_not_block_others(seeded):
    paused = await make_ready_run(seeded, 9, 100)
    other = await make_ready_run(seeded, 0, 1)
    async with unit_of_work() as s:
        await core.pause_run(s, paused, user_id=None)
    assert await claimed_runs(5) == [other]
