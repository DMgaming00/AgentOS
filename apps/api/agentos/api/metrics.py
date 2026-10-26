"""Operational metrics computed directly from persisted PostgreSQL rows (no synthetic data)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import Float, and_, case, cast, func, literal_column, select

from agentos.dependencies import SessionDep, WorkspaceDep
from agentos_core.db.models import (
    AgentDefinition,
    Event,
    ModelUsage,
    StepAttempt,
    ToolExecution,
    Worker,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.util import utcnow

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def _since(days: int):  # type: ignore[no-untyped-def]
    return utcnow() - timedelta(days=days)


@router.get("/overview")
async def overview(
    session: SessionDep, ctx: WorkspaceDep, days: int = Query(30, ge=1, le=365)
) -> dict[str, Any]:
    ws = ctx.workspace_id
    since = _since(days)
    by_status = dict(
        (
            await session.execute(
                select(WorkflowRun.status, func.count())
                .where(WorkflowRun.workspace_id == ws, WorkflowRun.created_at >= since)
                .group_by(WorkflowRun.status)
            )
        ).all()
    )
    duration = func.extract("epoch", WorkflowRun.completed_at - WorkflowRun.started_at) * 1000
    avg_ms, p50_ms, p95_ms = (
        await session.execute(
            select(
                func.avg(duration),
                func.percentile_cont(0.5).within_group(duration),
                func.percentile_cont(0.95).within_group(duration),
            ).where(
                WorkflowRun.workspace_id == ws,
                WorkflowRun.status == "succeeded",
                WorkflowRun.created_at >= since,
            )
        )
    ).one()
    cost = (
        await session.execute(
            select(func.coalesce(func.sum(WorkflowRun.cost_spent_usd), 0)).where(
                WorkflowRun.workspace_id == ws, WorkflowRun.created_at >= since
            )
        )
    ).scalar_one()
    simulated = (
        await session.execute(
            select(func.count())
            .select_from(ModelUsage)
            .join(WorkflowRun, WorkflowRun.id == ModelUsage.run_id)
            .where(
                WorkflowRun.workspace_id == ws,
                ModelUsage.cost_basis == "simulated",
                ModelUsage.recorded_at >= since,
            )
        )
    ).scalar_one()
    active_workers = (
        await session.execute(
            select(func.count())
            .select_from(Worker)
            .where(Worker.status == "active", Worker.last_heartbeat_at >= utcnow() - timedelta(seconds=30))
        )
    ).scalar_one()
    recent = (
        await session.execute(
            select(Event, WorkflowDefinition.name)
            .join(WorkflowRun, WorkflowRun.id == Event.run_id)
            .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
            .where(
                WorkflowRun.workspace_id == ws,
                Event.event_type.in_(
                    [
                        "run.created",
                        "run.completed",
                        "run.failed",
                        "run.cancelled",
                        "approval.requested",
                        "approval.resolved",
                        "step.failed",
                        "step.retrying",
                    ]
                ),
            )
            .order_by(Event.id.desc())
            .limit(20)
        )
    ).all()
    pending_approvals = (
        await session.execute(
            select(func.count())
            .select_from(WorkflowStep)
            .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
            .where(WorkflowRun.workspace_id == ws, WorkflowStep.status == "waiting_approval")
        )
    ).scalar_one()
    return {
        "window_days": days,
        "total_runs": sum(by_status.values()),
        "runs_by_status": by_status,
        "running": by_status.get("running", 0) + by_status.get("planning", 0),
        "waiting_approval": by_status.get("waiting_approval", 0),
        "succeeded": by_status.get("succeeded", 0),
        "failed": by_status.get("failed", 0),
        "cancelled": by_status.get("cancelled", 0),
        "avg_duration_ms": _f(avg_ms),
        "p50_duration_ms": _f(p50_ms),
        "p95_duration_ms": _f(p95_ms),
        "estimated_cost_usd": float(cost),
        "includes_simulated_cost": simulated > 0,
        "active_workers": active_workers,
        "steps_waiting_approval": pending_approvals,
        "recent_activity": [
            {
                "run_id": str(e.run_id),
                "workflow": name,
                "event_type": e.event_type,
                "timestamp": e.timestamp.isoformat(),
                "sequence_number": e.sequence_number,
                "node_key": e.payload.get("node_key"),
            }
            for e, name in recent
        ],
    }


@router.get("/usage")
async def usage(
    session: SessionDep, ctx: WorkspaceDep, days: int = Query(30, ge=1, le=365)
) -> dict[str, Any]:
    ws = ctx.workspace_id
    since = _since(days)
    base = (
        select(ModelUsage)
        .join(WorkflowRun, WorkflowRun.id == ModelUsage.run_id)
        .where(WorkflowRun.workspace_id == ws, ModelUsage.recorded_at >= since)
        .subquery()
    )
    lat = base.c.latency_ms
    rows = (
        await session.execute(
            select(
                base.c.provider,
                base.c.model,
                func.count(),
                func.sum(base.c.input_tokens),
                func.sum(base.c.output_tokens),
                func.sum(base.c.cached_input_tokens),
                func.count(base.c.input_tokens),
                func.sum(case((base.c.status == "failed", 1), else_=0)),
                func.percentile_cont(0.5).within_group(lat),
                func.percentile_cont(0.95).within_group(lat),
                func.sum(base.c.estimated_cost_usd),
            )
            .group_by(base.c.provider, base.c.model)
            .order_by(func.count().desc())
        )
    ).all()
    by_model = [
        {
            "provider": p,
            "model": m,
            "calls": n,
            "input_tokens": it,
            "output_tokens": ot,
            "cached_input_tokens": ct,
            "calls_with_usage": known,
            "failed_calls": failed,
            "latency_p50_ms": _f(p50),
            "latency_p95_ms": _f(p95),
            "estimated_cost_usd": _f(cost),
        }
        for p, m, n, it, ot, ct, known, failed, p50, p95, cost in rows
    ]
    daily = (
        await session.execute(
            select(
                func.date_trunc("day", base.c.recorded_at).label("d"),
                base.c.provider,
                func.count(),
                func.sum(base.c.input_tokens),
                func.sum(base.c.output_tokens),
                func.sum(base.c.estimated_cost_usd),
            )
            .group_by("d", base.c.provider)
            .order_by("d")
        )
    ).all()
    totals = {
        "calls": sum(r["calls"] for r in by_model),
        "input_tokens": sum(r["input_tokens"] or 0 for r in by_model),
        "output_tokens": sum(r["output_tokens"] or 0 for r in by_model),
        "calls_missing_usage": sum(r["calls"] - r["calls_with_usage"] for r in by_model),
    }
    providers: dict[str, int] = {}
    for r in by_model:
        providers[r["provider"]] = providers.get(r["provider"], 0) + r["calls"]
    return {
        "window_days": days,
        "totals": totals,
        "by_model": by_model,
        "provider_distribution": providers,
        "daily": [
            {
                "day": d.date().isoformat(),
                "provider": p,
                "calls": n,
                "input_tokens": it,
                "output_tokens": ot,
                "estimated_cost_usd": _f(c),
            }
            for d, p, n, it, ot, c in daily
        ],
    }


@router.get("/cost")
async def cost(session: SessionDep, ctx: WorkspaceDep, days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    ws = ctx.workspace_id
    since = _since(days)
    join = (
        select(ModelUsage, AgentDefinition.name.label("agent"))
        .join(WorkflowRun, WorkflowRun.id == ModelUsage.run_id)
        .outerjoin(AgentDefinition, AgentDefinition.id == ModelUsage.agent_definition_id)
        .where(WorkflowRun.workspace_id == ws, ModelUsage.recorded_at >= since)
        .subquery()
    )
    agent_name = func.coalesce(join.c.agent, literal_column("'planner'")).label("agent_name")
    by_agent = (
        await session.execute(
            select(agent_name, func.count(), func.sum(join.c.estimated_cost_usd))
            .group_by(agent_name)
            .order_by(func.sum(join.c.estimated_cost_usd).desc().nulls_last())
        )
    ).all()
    by_basis: dict[str, int] = dict(
        (await session.execute(select(join.c.cost_basis, func.count()).group_by(join.c.cost_basis))).all()
    )
    pricing_versions = (await session.execute(select(join.c.pricing_version).distinct())).scalars().all()
    expensive = (
        await session.execute(
            select(
                WorkflowRun.id,
                WorkflowDefinition.name,
                WorkflowRun.status,
                WorkflowRun.cost_spent_usd,
                WorkflowRun.model_calls,
                WorkflowRun.created_at,
            )
            .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
            .where(WorkflowRun.workspace_id == ws, WorkflowRun.created_at >= since)
            .order_by(WorkflowRun.cost_spent_usd.desc())
            .limit(10)
        )
    ).all()
    total = sum((c or Decimal(0)) for _, _, c in by_agent)
    return {
        "window_days": days,
        "total_estimated_cost_usd": float(total),
        "cost_basis_counts": by_basis,
        "pricing_versions": list(pricing_versions),
        "note": "Estimates from reported token usage and the versioned price table; not invoice amounts. "
        "'simulated' rows come from the mock provider.",
        "by_agent": [{"agent": a, "calls": n, "estimated_cost_usd": _f(c)} for a, n, c in by_agent],
        "most_expensive_runs": [
            {
                "run_id": str(i),
                "workflow": n,
                "status": s,
                "estimated_cost_usd": float(c),
                "model_calls": mc,
                "created_at": t.isoformat(),
            }
            for i, n, s, c, mc, t in expensive
        ],
    }


@router.get("/observability")
async def observability(
    session: SessionDep, ctx: WorkspaceDep, days: int = Query(7, ge=1, le=365)
) -> dict[str, Any]:
    ws = ctx.workspace_id
    since = _since(days)
    in_ws = and_(WorkflowRun.workspace_id == ws, WorkflowRun.created_at >= since)
    failures = (
        await session.execute(
            select(StepAttempt.error_type, func.count())
            .join(WorkflowRun, WorkflowRun.id == StepAttempt.run_id)
            .where(in_ws, StepAttempt.error_type.is_not(None))
            .group_by(StepAttempt.error_type)
            .order_by(func.count().desc())
        )
    ).all()
    attempt_status = dict(
        (
            await session.execute(
                select(StepAttempt.status, func.count())
                .join(WorkflowRun, WorkflowRun.id == StepAttempt.run_id)
                .where(in_ws)
                .group_by(StepAttempt.status)
            )
        ).all()
    )
    qw = cast(StepAttempt.queue_wait_ms, Float)
    q50, q95 = (
        await session.execute(
            select(func.percentile_cont(0.5).within_group(qw), func.percentile_cont(0.95).within_group(qw))
            .join(WorkflowRun, WorkflowRun.id == StepAttempt.run_id)
            .where(in_ws)
        )
    ).one()
    step_dur = func.extract("epoch", StepAttempt.finished_at - StepAttempt.started_at) * 1000
    by_type = (
        await session.execute(
            select(
                WorkflowStep.node_type,
                func.count(),
                func.percentile_cont(0.5).within_group(step_dur),
                func.percentile_cont(0.95).within_group(step_dur),
            )
            .join(WorkflowStep, WorkflowStep.id == StepAttempt.step_id)
            .join(WorkflowRun, WorkflowRun.id == StepAttempt.run_id)
            .where(in_ws, StepAttempt.status == "succeeded")
            .group_by(WorkflowStep.node_type)
        )
    ).all()
    tools = (
        await session.execute(
            select(ToolExecution.tool_name, ToolExecution.status, func.count())
            .join(WorkflowRun, WorkflowRun.id == ToolExecution.run_id)
            .where(in_ws)
            .group_by(ToolExecution.tool_name, ToolExecution.status)
        )
    ).all()
    tool_activity: dict[str, dict[str, int]] = {}
    for name, status, n in tools:
        tool_activity.setdefault(name, {})[status] = n
    recent_errors = (
        await session.execute(
            select(Event, WorkflowDefinition.name)
            .join(WorkflowRun, WorkflowRun.id == Event.run_id)
            .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
            .where(
                in_ws,
                Event.event_type.in_(
                    [
                        "step.failed",
                        "step.retrying",
                        "model.call.failed",
                        "tool.failed",
                        "tool.denied",
                        "run.failed",
                        "step.stale_result_rejected",
                    ]
                ),
            )
            .order_by(Event.id.desc())
            .limit(30)
        )
    ).all()
    timeline = (
        await session.execute(
            select(
                WorkflowRun.id,
                WorkflowDefinition.name,
                WorkflowRun.status,
                WorkflowRun.started_at,
                WorkflowRun.completed_at,
                WorkflowRun.cost_spent_usd,
            )
            .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
            .where(in_ws)
            .order_by(WorkflowRun.created_at.desc())
            .limit(25)
        )
    ).all()
    latency = (
        await session.execute(
            select(
                func.date_trunc("hour", ModelUsage.recorded_at).label("h"),
                ModelUsage.provider,
                func.percentile_cont(0.5).within_group(ModelUsage.latency_ms),
                func.percentile_cont(0.95).within_group(ModelUsage.latency_ms),
                func.count(),
            )
            .join(WorkflowRun, WorkflowRun.id == ModelUsage.run_id)
            .where(in_ws, ModelUsage.status == "succeeded")
            .group_by("h", ModelUsage.provider)
            .order_by("h")
        )
    ).all()
    workers = (
        (await session.execute(select(Worker).order_by(Worker.last_heartbeat_at.desc()).limit(20)))
        .scalars()
        .all()
    )
    now = utcnow()
    return {
        "window_days": days,
        "failure_breakdown": [{"error_type": t, "count": n} for t, n in failures],
        "attempts_by_status": attempt_status,
        "retries": attempt_status.get("failed", 0) + attempt_status.get("lease_expired", 0),
        "queue_wait_ms": {"p50": _f(q50), "p95": _f(q95)},
        "step_duration_by_type": [
            {"node_type": t, "count": n, "p50_ms": _f(a), "p95_ms": _f(b)} for t, n, a, b in by_type
        ],
        "tool_activity": tool_activity,
        "model_latency_hourly": [
            {"hour": h.isoformat(), "provider": p, "p50_ms": _f(a), "p95_ms": _f(b), "calls": n}
            for h, p, a, b, n in latency
        ],
        "recent_errors": [
            {
                "run_id": str(e.run_id),
                "workflow": name,
                "event_type": e.event_type,
                "timestamp": e.timestamp.isoformat(),
                "node_key": e.payload.get("node_key"),
                "error": e.payload.get("error"),
                "trace_id": e.trace_id,
            }
            for e, name in recent_errors
        ],
        "timeline": [
            {
                "run_id": str(i),
                "workflow": n,
                "status": s,
                "started_at": a.isoformat() if a else None,
                "completed_at": b.isoformat() if b else None,
                "cost_usd": float(c),
            }
            for i, n, s, a, b, c in timeline
        ],
        "workers": [
            {
                "id": w.id,
                "hostname": w.hostname,
                "status": w.status,
                "concurrency": w.concurrency,
                "in_flight": w.in_flight,
                "steps_completed": w.steps_completed,
                "last_heartbeat_at": w.last_heartbeat_at.isoformat(),
                "healthy": w.status == "active" and (now - w.last_heartbeat_at).total_seconds() < 30,
            }
            for w in workers
        ],
    }
