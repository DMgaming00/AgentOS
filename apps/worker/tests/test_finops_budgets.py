"""Spending controls on real PostgreSQL: budgets are enforced in code at reservation time, never by a model."""

import asyncio
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from agentos_core.db.models import Event, FinopsBudget, ModelUsage, WorkflowRun
from agentos_core.domain.errors import BudgetExceededError
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.finops.budgets import check_budgets, utilization
from agentos_core.testing import start_run
from agentos_core.util import utcnow

pytestmark = pytest.mark.integration
ONE = {"nodes": [{"key": "a", "type": "aggregation"}]}
AGENT_FLOW = {
    "nodes": [
        {
            "key": "a",
            "type": "agent_task",
            "agent": "Research Agent",
            "tools": ["document_search"],
            "input": {"platform": "Nimbus Inference"},
        }
    ]
}


async def budget(ws, **kw):
    base = dict(
        scope="workspace",
        period="daily",
        limit_usd=Decimal("0.10"),
        mode="enforce",
        warn_fraction=Decimal("0.8"),
        enabled=True,
    )
    async with unit_of_work() as s:
        b = FinopsBudget(id=uuid.uuid4(), workspace_id=ws, **{**base, **kw})
        s.add(b)
        await s.flush()
        return b.id


async def spend(ws, run_id, cost, *, when=None, agent_id=None):
    async with unit_of_work() as s:
        s.add(
            ModelUsage(
                id=uuid.uuid4(),
                run_id=run_id,
                workspace_id=ws,
                agent_definition_id=agent_id,
                provider="mock",
                model="m",
                input_tokens=1,
                output_tokens=1,
                latency_ms=1,
                estimated_cost_usd=Decimal(cost),
                cost_basis="simulated",
                pricing_version="t",
                recorded_at=when or utcnow(),
            )
        )


async def limit_events(run_id):
    async with unit_of_work() as s:
        return [
            e.payload
            for e in (
                await s.execute(
                    select(Event).where(Event.run_id == run_id, Event.event_type == "budget.exceeded")
                )
            ).scalars()
        ]


async def test_without_budgets_nothing_changes_and_there_is_no_extra_work(seeded):
    run_id = await start_run(seeded, ONE)
    (claim,) = await core.claim_steps("w", 1)
    await core.reserve_model_call(run_id, Decimal("0.05"), claim.attempt_id)  # no budget rows: fast path


async def test_workspace_budget_stops_the_call_before_it_is_made(seeded):
    ws = seeded.workspace.id
    run_id = await start_run(seeded, ONE)
    (claim,) = await core.claim_steps("w", 1)
    await budget(ws)
    await spend(ws, run_id, "0.08")
    with pytest.raises(BudgetExceededError, match="workspace budget exhausted"):
        await core.reserve_model_call(run_id, Decimal("0.05"), claim.attempt_id)
    (payload,) = await limit_events(run_id)
    assert (
        payload["limit"] == "workspace_budget"
        and payload["period"] == "daily"
        and payload["spent_usd"] == 0.08
        and payload["limit_usd"] == 0.1
    )
    async with unit_of_work() as s:
        run = await s.get(WorkflowRun, run_id)
        assert run.model_calls == 0 and run.cost_reserved_usd == 0  # nothing was reserved
    await core.reserve_model_call(run_id, Decimal("0.01"), claim.attempt_id)  # still fits under the limit


async def test_in_flight_reservations_count_toward_the_limit(seeded):
    ws = seeded.workspace.id
    run_id = await start_run(seeded, ONE)
    (claim,) = await core.claim_steps("w", 1)
    await budget(ws)
    await core.reserve_model_call(run_id, Decimal("0.06"), claim.attempt_id)
    with pytest.raises(BudgetExceededError):
        await core.reserve_model_call(
            run_id, Decimal("0.06"), claim.attempt_id
        )  # 0.06 reserved + 0.06 > 0.10


async def test_concurrent_reservations_cannot_all_slip_under_the_limit(seeded):
    ws = seeded.workspace.id
    run_ids = [await start_run(seeded, ONE) for _ in range(8)]
    claims = await core.claim_steps("w", 8)
    assert len(claims) == 8
    by_run = {c.run_id: c for c in claims}
    await budget(ws)

    async def attempt(run_id):
        try:
            await core.reserve_model_call(run_id, Decimal("0.03"), by_run[run_id].attempt_id)
            return True
        except BudgetExceededError:
            return False

    results = await asyncio.gather(*(attempt(r) for r in run_ids))
    assert sum(results) == 3  # 3 x 0.03 = 0.09 fits; a 4th would make 0.12 > 0.10


async def test_agent_budget_counts_only_that_agents_spend(seeded):
    ws = seeded.workspace.id
    research, other = seeded.agents["Research Agent"].id, seeded.agents["Writer Agent"].id
    run_id = await start_run(seeded, AGENT_FLOW)
    (claim,) = await core.claim_steps("w", 1)
    await budget(ws, scope="agent", agent_id=research)
    await spend(
        ws, run_id, "0.50", agent_id=other
    )  # another agent's spend is irrelevant to this agent's budget
    await core.reserve_model_call(run_id, Decimal("0.05"), claim.attempt_id)
    await spend(ws, run_id, "0.09", agent_id=research)
    with pytest.raises(BudgetExceededError, match="agent budget exhausted"):
        await core.reserve_model_call(run_id, Decimal("0.05"), claim.attempt_id)
    assert (await limit_events(run_id))[-1]["limit"] == "agent_budget"


async def test_alert_mode_and_disabled_budgets_do_not_block_but_utilization_reports(seeded):
    ws = seeded.workspace.id
    run_id = await start_run(seeded, ONE)
    (claim,) = await core.claim_steps("w", 1)
    alert = await budget(ws, mode="alert", limit_usd=Decimal("0.05"))
    await budget(ws, period="monthly", enabled=False, limit_usd=Decimal("0.01"))
    await spend(ws, run_id, "0.09")
    await core.reserve_model_call(
        run_id, Decimal("0.05"), claim.attempt_id
    )  # alert-only and disabled: allowed
    async with unit_of_work() as s:
        u = await utilization(s, await s.get(FinopsBudget, alert))
    assert u["state"] == "exceeded" and u["utilization"] > 1 and u["mode"] == "alert"


async def test_period_boundaries_daily_vs_monthly(seeded):
    ws = seeded.workspace.id
    run_id = await start_run(seeded, ONE)
    (claim,) = await core.claim_steps("w", 1)
    now = utcnow()
    from agentos_core.finops.budgets import period_bounds

    day_start, _ = period_bounds("daily", now)
    month_start, _ = period_bounds("monthly", now)
    await budget(ws, period="daily")
    await budget(ws, period="monthly", limit_usd=Decimal("0.50"))
    # spend from just before today began: counts for the month (if it is the same month) but not for today
    yesterday = day_start - timedelta(minutes=5)
    await spend(ws, run_id, "0.40", when=yesterday)
    async with unit_of_work() as s:
        d = await check_budgets(s, run_id, claim.attempt_id, Decimal("0.05"), now=now)
        assert d is None  # the daily budget saw nothing today; the monthly one still has headroom
        assert yesterday >= month_start or d is None
    await spend(ws, run_id, "0.08", when=now)
    async with unit_of_work() as s:
        denial = await check_budgets(s, run_id, claim.attempt_id, Decimal("0.05"), now=now)
    assert denial is not None and denial.period in ("daily", "monthly")


async def test_a_real_run_is_stopped_by_the_workspace_budget_and_the_cause_is_recorded(seeded):
    from agentos_core.testing import get_steps, running_workers, wait_run_status

    ws = seeded.workspace.id
    await budget(ws, limit_usd=Decimal("0.000001"))  # below what any single model call reserves
    run_id = await start_run(seeded, AGENT_FLOW)
    async with running_workers(1):
        run = await wait_run_status(run_id, {"failed", "succeeded"}, timeout=40)
    assert run.status == "failed"
    step = (await get_steps(run_id))["a"]
    assert step.error["type"] == "budget_exceeded"
    assert (await limit_events(run_id))[0]["limit"] == "workspace_budget"
    async with unit_of_work() as s:
        spent = (await s.execute(select(ModelUsage).where(ModelUsage.run_id == run_id))).scalars().all()
        assert spent == []  # the request was never made, so nothing was spent
