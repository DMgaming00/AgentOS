"""Engine integration tests against real PostgreSQL (row locks, SKIP LOCKED, fencing)."""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from agentos_core.db.models import Event, StepAttempt, WorkflowStep
from agentos_core.domain.errors import InvalidInputError, ProviderTimeoutError
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import get_run, get_steps, start_run
from agentos_core.util import utcnow

pytestmark = pytest.mark.integration


def calc_node(key, deps=(), **kw):
    return {
        "key": key,
        "type": "tool_task",
        "tool": "calculator",
        "depends_on": list(deps),
        "arguments": {"operation": "add", "values": [1, 2]},
        **kw,
    }


async def force_lease_expiry(step_id):
    async with unit_of_work() as s:
        await s.execute(
            update(WorkflowStep)
            .where(WorkflowStep.id == step_id)
            .values(lease_expires_at=utcnow() - timedelta(seconds=1))
        )


async def force_retry_due(step_id):
    async with unit_of_work() as s:
        await s.execute(
            update(WorkflowStep)
            .where(WorkflowStep.id == step_id)
            .values(next_run_at=utcnow() - timedelta(seconds=1))
        )


async def test_concurrent_claims_never_double_claim(seeded):
    nodes = [calc_node(f"n{i:02d}") for i in range(30)]
    run_id = await start_run(seeded, {"nodes": nodes, "budget": {"max_concurrency": 64}})
    results = await asyncio.gather(*(core.claim_steps(f"w{i}", 3) for i in range(15)))
    claimed = [c.step_id for batch in results for c in batch]
    assert len(claimed) == len(set(claimed)), "a step was claimed twice"
    # Some claimers may skip a locked run; drain the rest sequentially.
    while more := await core.claim_steps("drain", 50):
        claimed += [c.step_id for c in more]
    assert len(set(claimed)) == 30
    steps = await get_steps(run_id)
    assert all(s.status == "running" and s.lease_token for s in steps.values())
    tokens = [s.lease_token for s in steps.values()]
    assert len(set(tokens)) == 30


async def test_max_concurrency_per_run_is_enforced(seeded):
    run_id = await start_run(
        seeded, {"nodes": [calc_node(f"n{i}") for i in range(6)], "budget": {"max_concurrency": 2}}
    )
    first = await core.claim_steps("w1", 10)
    assert len(first) == 2
    assert await core.claim_steps("w2", 10) == []
    await core.complete_step(first[0].step_id, first[0].lease_token, {"ok": 1})
    assert len(await core.claim_steps("w2", 10)) == 1
    assert (await get_run(run_id)).status == "running"


async def test_dependency_resolution_diamond(seeded):
    run_id = await start_run(
        seeded,
        {"nodes": [calc_node("a"), calc_node("b", ["a"]), calc_node("c", ["a"]), calc_node("d", ["b", "c"])]},
    )
    steps = await get_steps(run_id)
    assert {k: s.status for k, s in steps.items()} == {
        "a": "ready",
        "b": "pending",
        "c": "pending",
        "d": "pending",
    }
    (a,) = await core.claim_steps("w", 10)
    await core.complete_step(a.step_id, a.lease_token, {"v": 1})
    bc = await core.claim_steps("w", 10)
    assert sorted(c.node_key for c in bc) == ["b", "c"]
    assert all(c.dependency_outputs == {"a": {"v": 1}} for c in bc)
    await core.complete_step(bc[0].step_id, bc[0].lease_token, {})
    assert await core.claim_steps("w", 10) == []  # d still waits for the other branch
    await core.complete_step(bc[1].step_id, bc[1].lease_token, {})
    (d,) = await core.claim_steps("w", 10)
    await core.complete_step(d.step_id, d.lease_token, {"final": True})
    run = await get_run(run_id)
    assert run.status == "succeeded" and run.output == {"d": {"final": True}} and run.completed_at


async def test_stale_worker_cannot_overwrite_newer_result(seeded):
    run_id = await start_run(
        seeded, {"nodes": [calc_node("a", retry={"max_attempts": 3, "backoff_base_seconds": 0})]}
    )
    (old,) = await core.claim_steps("old-worker", 1)
    await force_lease_expiry(old.step_id)
    assert await core.expire_leases() == 1
    steps = await get_steps(run_id)
    assert steps["a"].status == "retry_wait" and steps["a"].failure_count == 1
    await force_retry_due(old.step_id)
    await core.promote_retries()
    (new,) = await core.claim_steps("new-worker", 1)
    assert new.attempt_number == 2 and new.lease_token != old.lease_token

    with pytest.raises(core.StaleLeaseError):
        await core.complete_step(old.step_id, old.lease_token, {"from": "old"})
    with pytest.raises(core.StaleLeaseError):
        await core.fail_step(old.step_id, old.lease_token, InvalidInputError("late"))
    step = (await get_steps(run_id))["a"]
    assert step.status == "running" and step.lease_token == new.lease_token and step.output is None

    await core.complete_step(new.step_id, new.lease_token, {"from": "new"})
    with pytest.raises(core.StaleLeaseError):
        await core.complete_step(old.step_id, old.lease_token, {"from": "old-again"})
    step = (await get_steps(run_id))["a"]
    assert step.status == "succeeded" and step.output == {"from": "new"}
    async with unit_of_work() as s:
        attempts = (
            (
                await s.execute(
                    select(StepAttempt)
                    .where(StepAttempt.step_id == step.id)
                    .order_by(StepAttempt.attempt_number)
                )
            )
            .scalars()
            .all()
        )
        rejected = (
            (
                await s.execute(
                    select(Event).where(
                        Event.run_id == run_id, Event.event_type == "step.stale_result_rejected"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [a.status for a in attempts] == ["lease_expired", "succeeded"]
    assert (
        attempts[0].error_type == "worker_lost"
        and "late_result_rejected_at" in attempts[0].execution_metadata
    )
    assert len(rejected) == 3


async def test_heartbeat_renews_and_reports_lost_leases(seeded):
    await start_run(seeded, {"nodes": [calc_node("a"), calc_node("b")]})
    claims = await core.claim_steps("w1", 2)
    before = {c.step_id: c.lease_expires_at for c in claims}
    hb = await core.heartbeat(
        "w1",
        [c.lease_token for c in claims] + [uuid.uuid4()],
        hostname="h",
        pid=1,
        concurrency=2,
        lease_ttl=30,
    )
    assert hb.renewed == {c.lease_token for c in claims} and len(hb.lost) == 1
    steps = await get_steps(claims[0].run_id)
    assert all(s.lease_expires_at > before[s.id] for s in steps.values())
    # A different worker cannot renew someone else's lease.
    hb2 = await core.heartbeat(
        "intruder", [claims[0].lease_token], hostname="h", pid=2, concurrency=1, lease_ttl=30
    )
    assert hb2.renewed == set()


async def test_transient_failure_retries_with_backoff_then_permanent_fails_run(seeded):
    retry = {"max_attempts": 3, "backoff_base_seconds": 5, "backoff_max_seconds": 5, "jitter": False}
    run_id = await start_run(
        seeded, {"nodes": [calc_node("a", retry=retry), calc_node("b", ["a"]), calc_node("c")]}
    )
    claims = {c.node_key: c for c in await core.claim_steps("w", 10)}
    status = await core.fail_step(claims["a"].step_id, claims["a"].lease_token, ProviderTimeoutError("slow"))
    assert status == "retry_wait"
    a = (await get_steps(run_id))["a"]
    assert 4.5 < (a.next_run_at - utcnow()).total_seconds() <= 5.0
    await core.promote_retries()
    assert (await get_steps(run_id))["a"].status == "retry_wait"  # not due yet
    await force_retry_due(a.id)
    assert await core.promote_retries() == 1
    (again,) = [c for c in await core.claim_steps("w", 10) if c.node_key == "a"]
    status = await core.fail_step(again.step_id, again.lease_token, InvalidInputError("bad input"))
    assert status == "failed"
    steps = await get_steps(run_id)
    assert steps["b"].status == "skipped" and steps["c"].status == "running"
    assert (await get_run(run_id)).status == "running"  # waits for in-flight sibling
    await core.complete_step(claims["c"].step_id, claims["c"].lease_token, {})
    run = await get_run(run_id)
    assert run.status == "failed" and run.error["failed_steps"] == ["a"]


async def test_max_attempts_exhausted(seeded):
    retry = {"max_attempts": 2, "backoff_base_seconds": 0, "backoff_max_seconds": 0}
    run_id = await start_run(seeded, {"nodes": [calc_node("a", retry=retry)]})
    for expected in ("retry_wait", "failed"):
        await core.promote_retries()
        (c,) = await core.claim_steps("w", 1)
        assert await core.fail_step(c.step_id, c.lease_token, ProviderTimeoutError("t")) == expected
    step = (await get_steps(run_id))["a"]
    assert step.failure_count == 2 and step.attempt_count == 2
    assert (await get_run(run_id)).status == "failed"


async def test_lease_expiry_exhausts_attempts(seeded):
    run_id = await start_run(seeded, {"nodes": [calc_node("a", retry={"max_attempts": 1})]})
    (c,) = await core.claim_steps("w", 1)
    await force_lease_expiry(c.step_id)
    await core.expire_leases()
    step = (await get_steps(run_id))["a"]
    assert step.status == "failed" and step.error["type"] == "worker_lost"


async def test_cancellation_queued_and_running(seeded):
    run_id = await start_run(
        seeded,
        {"nodes": [calc_node("a"), calc_node("b"), calc_node("c", ["a"])], "budget": {"max_concurrency": 1}},
    )
    (running,) = await core.claim_steps("w", 1)
    async with unit_of_work() as s:
        run = await core.cancel_run(s, run_id, user_id=seeded.users["admin"].id)
        assert run.status == "cancelled"
    steps = await get_steps(run_id)
    others = {k: v.status for k, v in steps.items() if k != running.node_key}
    assert set(others.values()) == {"cancelled"}
    assert steps[running.node_key].status == "running"
    assert await core.claim_steps("w", 5) == []  # nothing new starts
    hb = await core.heartbeat("w", [running.lease_token], hostname="h", pid=1, concurrency=1, lease_ttl=10)
    assert hb.cancelled == {running.lease_token}
    # Non-interruptible work finishes: its result is kept but the step is marked cancelled.
    await core.complete_step(running.step_id, running.lease_token, {"late": True})
    steps = await get_steps(run_id)
    assert steps[running.node_key].status == "cancelled" and steps[running.node_key].output == {"late": True}
    run = await get_run(run_id)
    assert run.status == "cancelled" and run.completed_at is not None
    async with unit_of_work() as s:
        with pytest.raises(core.EngineError):
            await core.cancel_run(s, run_id, user_id=None)


async def test_event_sequence_is_contiguous_and_ordered(seeded):
    run_id = await start_run(
        seeded, {"nodes": [calc_node(f"n{i}") for i in range(8)], "budget": {"max_concurrency": 8}}
    )
    claims = await core.claim_steps("w", 8)
    await asyncio.gather(
        *(core.complete_step(c.step_id, c.lease_token, {"i": i}) for i, c in enumerate(claims))
    )
    async with unit_of_work() as s:
        seqs = (
            await s.execute(
                select(Event.sequence_number, Event.event_type)
                .where(Event.run_id == run_id)
                .order_by(Event.id)
            )
        ).all()
    numbers = [n for n, _ in seqs]
    assert numbers == list(range(1, len(numbers) + 1))
    types = [t for _, t in seqs]
    assert types[0] == "run.created" and types[-1] == "run.completed"
    assert types.count("step.completed") == 8


async def test_run_deadline_enforced(seeded):
    run_id = await start_run(seeded, {"nodes": [calc_node("a"), calc_node("b", ["a"])]})
    (c,) = await core.claim_steps("w", 1)
    async with unit_of_work() as s:
        from agentos_core.db.models import WorkflowRun

        await s.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(deadline_at=utcnow() - timedelta(seconds=1))
        )
    assert await core.enforce_deadlines() == 1
    run = await get_run(run_id)
    steps = await get_steps(run_id)
    assert run.status == "failed" and steps["a"].status == "failed" and steps["b"].status == "skipped"
    with pytest.raises(core.StaleLeaseError):
        await core.complete_step(c.step_id, c.lease_token, {})


async def test_model_call_budget_reservation_is_atomic(seeded):
    run_id = await start_run(
        seeded, {"nodes": [calc_node("a")], "budget": {"max_model_calls": 5, "max_cost_usd": 1}}
    )
    from decimal import Decimal

    from agentos_core.domain.errors import BudgetExceededError

    outcomes = await asyncio.gather(
        *(core.reserve_model_call(run_id, Decimal("0.1")) for _ in range(12)), return_exceptions=True
    )
    ok = [o for o in outcomes if o is None]
    assert len(ok) == 5 and all(isinstance(o, BudgetExceededError) for o in outcomes if o is not None)
    run = await get_run(run_id)
    assert run.model_calls == 5 and run.cost_reserved_usd == Decimal("0.5")
