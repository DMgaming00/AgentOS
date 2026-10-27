"""Spending limits enforced in code, before a model request is made.

A budget limits the estimated spend of a workspace, or of one agent, over a UTC calendar day or month. Spend is the sum of
recorded ``model_usage`` cost in the period plus the budget currently *reserved* by in-flight model calls, so concurrent
calls cannot all slip under the limit. For workspaces with an enforcing budget the check and the reservation are
serialized with a per-workspace advisory lock. Nothing here consults a model.

Known approximations (see limitations): usage rows whose cost is unknown (NULL) contribute nothing; calls made outside a
workflow run (Autopilot diagnosis/optimizer) count toward spend but are not themselves stopped by these budgets (they have
their own per-cycle cap).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import (
    FinopsBudget,
    ModelUsage,
    StepAttempt,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.util import utcnow


@dataclass(frozen=True)
class Denial:
    budget_id: uuid.UUID
    scope: str
    period: str
    spent: Decimal
    in_flight: Decimal
    estimate: Decimal
    limit: Decimal

    @property
    def limit_name(self) -> str:
        return f"{self.scope}_budget"


def period_bounds(period: str, now: datetime) -> tuple[datetime, datetime]:
    now = now.astimezone(UTC)
    if period == "daily":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (start + timedelta(days=32)).replace(day=1)
    return start, end


async def spend_since(
    session: AsyncSession, workspace_id: uuid.UUID, since: datetime, agent_id: uuid.UUID | None = None
) -> Decimal:
    ws = func.coalesce(ModelUsage.workspace_id, WorkflowRun.workspace_id)
    q = (
        select(func.coalesce(func.sum(ModelUsage.estimated_cost_usd), 0))
        .select_from(ModelUsage)
        .outerjoin(WorkflowRun, WorkflowRun.id == ModelUsage.run_id)
        .where(ws == workspace_id, ModelUsage.recorded_at >= since)
    )
    if agent_id is not None:
        q = q.where(ModelUsage.agent_definition_id == agent_id)
    return Decimal((await session.execute(q)).scalar_one() or 0)


async def in_flight_reserved(
    session: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID | None = None
) -> Decimal:
    q = (
        select(func.coalesce(func.sum(StepAttempt.reserved_usd), 0))
        .select_from(StepAttempt)
        .join(WorkflowStep, WorkflowStep.id == StepAttempt.step_id)
        .join(WorkflowRun, WorkflowRun.id == StepAttempt.run_id)
        .where(WorkflowRun.workspace_id == workspace_id, StepAttempt.reserved_usd > 0)
    )
    if agent_id is not None:
        q = q.where(WorkflowStep.agent_definition_id == agent_id)
    return Decimal((await session.execute(q)).scalar_one() or 0)


async def check_budgets(
    session: AsyncSession,
    run_id: uuid.UUID,
    attempt_id: uuid.UUID | None,
    estimate: Decimal,
    *,
    now: datetime | None = None,
) -> Denial | None:
    """Return a ``Denial`` if reserving ``estimate`` would push an enforcing budget over its limit."""
    now = now or utcnow()
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        return None
    agent_id: uuid.UUID | None = None
    if attempt_id is not None:
        agent_id = (
            await session.execute(
                select(WorkflowStep.agent_definition_id)
                .join(StepAttempt, StepAttempt.step_id == WorkflowStep.id)
                .where(StepAttempt.id == attempt_id)
            )
        ).scalar_one_or_none()
    budgets = list(
        (
            await session.execute(
                select(FinopsBudget)
                .where(
                    FinopsBudget.workspace_id == run.workspace_id,
                    FinopsBudget.enabled.is_(True),
                    FinopsBudget.mode == "enforce",
                    (FinopsBudget.scope == "workspace") | (FinopsBudget.agent_id == agent_id),
                )
                .order_by(FinopsBudget.scope.desc(), FinopsBudget.period)
            )
        ).scalars()
    )
    if not budgets:
        return None  # fast path: no enforcing budget, no lock, no extra queries
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": f"finops:{run.workspace_id}"}
    )
    for b in budgets:
        start, _ = period_bounds(b.period, now)
        scope_agent = b.agent_id if b.scope == "agent" else None
        spent = await spend_since(session, run.workspace_id, start, scope_agent)
        reserved = await in_flight_reserved(session, run.workspace_id, scope_agent)
        if spent + reserved + estimate > b.limit_usd:
            return Denial(b.id, b.scope, b.period, spent, reserved, estimate, Decimal(b.limit_usd))
    return None


# ----------------------------------------------------------------------------- management and utilization


async def utilization(
    session: AsyncSession, budget: FinopsBudget, *, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    start, end = period_bounds(budget.period, now)
    scope_agent = budget.agent_id if budget.scope == "agent" else None
    spent = await spend_since(session, budget.workspace_id, start, scope_agent)
    reserved = await in_flight_reserved(session, budget.workspace_id, scope_agent)
    limit = Decimal(budget.limit_usd)
    fraction = float((spent + reserved) / limit)
    elapsed = (now - start).total_seconds() / (end - start).total_seconds()
    projected = (
        float(spent) / elapsed if elapsed > 0.02 else None
    )  # too little of the period elapsed to extrapolate
    state = "exceeded" if fraction >= 1 else "warning" if fraction >= float(budget.warn_fraction) else "ok"
    return {
        "id": str(budget.id),
        "scope": budget.scope,
        "agent_id": str(budget.agent_id) if budget.agent_id else None,
        "period": budget.period,
        "mode": budget.mode,
        "enabled": budget.enabled,
        "limit_usd": float(limit),
        "warn_fraction": float(budget.warn_fraction),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "spent_usd": float(spent),
        "reserved_usd": float(reserved),
        "utilization": fraction,
        "state": state,
        "projected_period_spend_usd": projected,
        "projection_note": "linear extrapolation of spend so far in the period; a projection, not a forecast"
        if projected is not None
        else "too little of the period has elapsed to project",
    }
