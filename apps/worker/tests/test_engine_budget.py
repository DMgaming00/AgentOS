"""Budget reservations are released whenever an attempt ends, however it ends (found by the Chaos Lab: every worker
crash used to leave its in-flight reservation on the run forever)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from agentos_core.db.models import StepAttempt, WorkflowRun, WorkflowStep
from agentos_core.domain.errors import ProviderUnavailableError
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import start_run
from agentos_core.util import utcnow

pytestmark = pytest.mark.integration
ONE = {
    "nodes": [{"key": "a", "type": "aggregation", "retry": {"max_attempts": 3, "backoff_base_seconds": 0.1}}]
}


async def claimed(seeded):
    run_id = await start_run(seeded, ONE)
    (claim,) = await core.claim_steps("budget-test", 1)
    await core.reserve_model_call(run_id, Decimal("0.05"), claim.attempt_id)
    return run_id, claim


async def lapse(run_id):
    """The worker is gone: its lease is now in the past."""
    async with unit_of_work() as s:
        await s.execute(
            update(WorkflowStep)
            .where(WorkflowStep.run_id == run_id, WorkflowStep.status == "running")
            .values(lease_expires_at=utcnow() - timedelta(seconds=1))
        )
    assert await core.expire_leases() == 1


async def state(run_id, attempt_id):
    async with unit_of_work() as s:
        run = await s.get(WorkflowRun, run_id)
        attempt = await s.get(StepAttempt, attempt_id)
        return run.cost_reserved_usd, attempt.reserved_usd


async def test_a_reservation_is_tracked_on_the_attempt(seeded):
    run_id, claim = await claimed(seeded)
    assert await state(run_id, claim.attempt_id) == (Decimal("0.05"), Decimal("0.05"))


async def test_settling_releases_it_normally(seeded):
    run_id, claim = await claimed(seeded)
    async with unit_of_work() as s:
        await core.settle_model_call(s, run_id, Decimal("0.05"), Decimal("0.01"), claim.attempt_id)
    assert await state(run_id, claim.attempt_id) == (Decimal("0"), Decimal("0"))


async def test_a_worker_that_dies_mid_call_does_not_leak_its_reservation(seeded):
    run_id, claim = await claimed(seeded)
    await lapse(run_id)
    assert await state(run_id, claim.attempt_id) == (Decimal("0"), Decimal("0"))
    async with unit_of_work() as s:
        run = await s.get(WorkflowRun, run_id)
    assert run.cost_spent_usd == 0  # nothing was charged for a call that never finished


async def test_failure_cancellation_and_completion_release_unsettled_reservations(seeded):
    run_id, claim = await claimed(seeded)
    await core.fail_step(claim.step_id, claim.lease_token, ProviderUnavailableError("down"))
    assert await state(run_id, claim.attempt_id) == (Decimal("0"), Decimal("0"))

    run_id, claim = await claimed(seeded)
    await core.complete_step(claim.step_id, claim.lease_token, {"ok": True})
    assert await state(run_id, claim.attempt_id) == (Decimal("0"), Decimal("0"))

    run_id, claim = await claimed(seeded)
    await core.mark_step_cancelled(claim.step_id, claim.lease_token, note="stop")
    assert await state(run_id, claim.attempt_id) == (Decimal("0"), Decimal("0"))


async def test_repeated_crashes_do_not_exhaust_the_budget(seeded):
    """Four crashes would have leaked $0.20; with a $0.12 cap the third call would already have been refused."""
    many = {
        "nodes": [
            {"key": "a", "type": "aggregation", "retry": {"max_attempts": 6, "backoff_base_seconds": 0.1}}
        ]
    }
    run_id = await start_run(seeded, {**many, "budget": {"max_cost_usd": 0.12}})
    for _ in range(4):
        async with unit_of_work() as s:  # make the retry claimable immediately
            await s.execute(
                update(WorkflowStep)
                .where(WorkflowStep.run_id == run_id)
                .values(next_run_at=utcnow() - timedelta(seconds=1))
            )
        await core.promote_retries()
        claims = await core.claim_steps("budget-test", 1)
        assert claims, "the step must be claimable again after each recovery"
        await core.reserve_model_call(
            run_id, Decimal("0.05"), claims[0].attempt_id
        )  # raises if budget had leaked
        await lapse(run_id)
    async with unit_of_work() as s:
        run = await s.get(WorkflowRun, run_id)
        attempts = (await s.execute(select(StepAttempt).where(StepAttempt.run_id == run_id))).scalars().all()
    assert run.cost_reserved_usd == 0 and [a.status for a in attempts].count("lease_expired") == 4
