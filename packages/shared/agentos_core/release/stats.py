"""Live samples for one agent version: finished steps of real (purpose 'workflow') runs in one environment."""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import ModelUsage, ToolExecution, WorkflowRun, WorkflowStep
from agentos_core.release.rules import Arm, Sample


async def load_arm(session: AsyncSession, version_id: uuid.UUID, *, environment: str, since: datetime) -> Arm:
    rows = (
        await session.execute(
            select(WorkflowStep.id, WorkflowStep.status, WorkflowStep.started_at, WorkflowStep.completed_at)
            .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
            .where(
                WorkflowStep.agent_version_id == version_id,
                WorkflowRun.environment == environment,
                WorkflowRun.purpose == "workflow",
                WorkflowStep.status.in_(["succeeded", "failed"]),
                WorkflowStep.completed_at >= since,
            )
            .order_by(WorkflowStep.completed_at)
        )
    ).all()
    if not rows:
        return Arm([], 0)
    ids = [r[0] for r in rows]
    cost: dict[uuid.UUID, float] = defaultdict(float)
    known: set[uuid.UUID] = set()
    spend = (
        await session.execute(
            select(ModelUsage.step_id, func.sum(ModelUsage.estimated_cost_usd))
            .where(ModelUsage.step_id.in_(ids), ModelUsage.estimated_cost_usd.is_not(None))
            .group_by(ModelUsage.step_id)
        )
    ).all()
    for step_id, total in spend:
        if step_id is not None and total is not None:
            cost[step_id] = float(total)
            known.add(step_id)
    denied = (
        await session.execute(
            select(func.count())
            .select_from(ToolExecution)
            .where(
                ToolExecution.step_id.in_(ids),
                ToolExecution.status == "denied",
                # a budget stop is spending control, not a policy violation
                func.coalesce(ToolExecution.error["type"].astext, "") != "budget_exceeded",
            )
        )
    ).scalar_one()
    samples = [
        Sample(
            at=completed,
            ok=status == "succeeded",
            latency_ms=((completed - started).total_seconds() * 1000) if started and completed else None,
            cost_usd=cost[sid] if sid in known else None,
        )
        for sid, status, started, completed in rows
    ]
    return Arm(samples, int(denied))
