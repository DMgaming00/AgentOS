"""Usage analytics over a window: cost per agent / workflow / model / tool, retry and failed-run cost, trends.

The aggregation is a pure function of plain rows (``summarize``) so it can be tested with hand-built data; ``load_inputs``
reads the rows from PostgreSQL. All amounts are estimates (see ``finops.ESTIMATE_LABEL``). Costs that are unknown (the
provider reported no usage) are counted separately and never treated as zero.
"""

from __future__ import annotations

import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import (
    AgentDefinition,
    ModelUsage,
    StepAttempt,
    ToolExecution,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.finops import ESTIMATE_LABEL
from agentos_core.util import utcnow
from openeval.metrics import nearest_rank

FAILED_RUN = ("failed", "cancelled", "timed_out")
MAX_ROWS = 200_000


@dataclass(frozen=True)
class UsageRow:
    at: datetime
    run_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    agent: str
    workflow: str
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    cost: Decimal | None
    basis: str
    pricing_version: str
    status: str
    purpose: str


@dataclass(frozen=True)
class RunRow:
    id: uuid.UUID
    workflow: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class StepRow:
    id: uuid.UUID
    run_id: uuid.UUID
    agent_id: uuid.UUID | None
    status: str


@dataclass(frozen=True)
class AttemptRow:
    id: uuid.UUID
    run_id: uuid.UUID
    agent_id: uuid.UUID | None
    status: str
    started_at: datetime
    queue_wait_ms: int | None


@dataclass(frozen=True)
class ToolRow:
    run_id: uuid.UUID
    agent_id: uuid.UUID | None
    tool: str
    status: str
    args_hash: str
    read_only: bool = True


@dataclass
class Inputs:
    usage: list[UsageRow] = field(default_factory=list)
    runs: list[RunRow] = field(default_factory=list)
    steps: list[StepRow] = field(default_factory=list)
    attempts: list[AttemptRow] = field(default_factory=list)
    tools: list[ToolRow] = field(default_factory=list)
    truncated: bool = False


def _f(v: Decimal | float | None) -> float | None:
    return None if v is None else float(v)


def _sum(rows: list[UsageRow]) -> Decimal:
    return sum((r.cost for r in rows if r.cost is not None), Decimal(0))


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def summarize(inp: Inputs, *, now: datetime, days: int) -> dict[str, Any]:
    usage = inp.usage
    total = _sum(usage)
    unknown = sum(1 for r in usage if r.cost is None)
    run_status = {r.id: r.status for r in inp.runs}
    failed_attempt_ids = {a.id for a in inp.attempts if a.status in ("failed", "lease_expired")}

    # by agent
    steps_by_agent: dict[uuid.UUID | None, list[StepRow]] = defaultdict(list)
    for s in inp.steps:
        steps_by_agent[s.agent_id].append(s)
    usage_by_agent: dict[tuple[uuid.UUID | None, str], list[UsageRow]] = defaultdict(list)
    for r in usage:
        usage_by_agent[(r.agent_id, r.agent)].append(r)
    by_agent: list[dict[str, Any]] = []
    for (aid, name), rows in usage_by_agent.items():
        steps = steps_by_agent.get(aid, [])
        ok = sum(s.status == "succeeded" for s in steps)
        bad = sum(s.status == "failed" for s in steps)
        cost = _sum(rows)
        retry = _sum([r for r in rows if r.attempt_id in failed_attempt_ids])
        by_agent.append(
            {
                "agent_id": str(aid) if aid else None,
                "agent": name,
                "calls": len(rows),
                "estimated_cost_usd": _f(cost),
                "share": _ratio(float(cost), float(total)),
                "input_tokens": sum(r.input_tokens or 0 for r in rows),
                "output_tokens": sum(r.output_tokens or 0 for r in rows),
                "successful_steps": ok,
                "failed_steps": bad,
                "cost_per_successful_step_usd": _f(cost / ok) if ok else None,
                "retry_cost_usd": _f(retry),
                "failed_run_cost_usd": _f(
                    _sum([r for r in rows if r.run_id and run_status.get(r.run_id) in FAILED_RUN])
                ),
            }
        )
    by_agent.sort(key=lambda x: -(x["estimated_cost_usd"] or 0))

    # by workflow
    by_wf: dict[str, list[UsageRow]] = defaultdict(list)
    for r in usage:
        by_wf[r.workflow].append(r)
    runs_by_wf: dict[str, list[RunRow]] = defaultdict(list)
    for run in inp.runs:
        runs_by_wf[run.workflow].append(run)
    by_workflow: list[dict[str, Any]] = []
    for wf, rows in by_wf.items():
        runs = runs_by_wf.get(wf, [])
        ok = sum(r.status == "succeeded" for r in runs)
        cost = _sum(rows)
        by_workflow.append(
            {
                "workflow": wf,
                "runs": len(runs),
                "succeeded_runs": ok,
                "estimated_cost_usd": _f(cost),
                "cost_per_succeeded_run_usd": _f(cost / ok) if ok else None,
                "failed_run_cost_usd": _f(
                    _sum([r for r in rows if r.run_id and run_status.get(r.run_id) in FAILED_RUN])
                ),
            }
        )
    by_workflow.sort(key=lambda x: -(x["estimated_cost_usd"] or 0))

    # by model
    by_model_rows: dict[tuple[str, str], list[UsageRow]] = defaultdict(list)
    for r in usage:
        by_model_rows[(r.provider, r.model)].append(r)
    by_model: list[dict[str, Any]] = []
    for (prov, model), rows in by_model_rows.items():
        lat = [float(r.latency_ms) for r in rows]
        by_model.append(
            {
                "provider": prov,
                "model": model,
                "calls": len(rows),
                "failed_calls": sum(r.status != "succeeded" for r in rows),
                "input_tokens": sum(r.input_tokens or 0 for r in rows),
                "output_tokens": sum(r.output_tokens or 0 for r in rows),
                "estimated_cost_usd": _f(_sum(rows)),
                "latency_p50_ms": nearest_rank(lat, 0.5),
                "latency_p95_ms": nearest_rank(lat, 0.95),
                "cost_bases": sorted({r.basis for r in rows}),
            }
        )
    by_model.sort(key=lambda x: -(x["estimated_cost_usd"] or 0))

    purposes: dict[str, Decimal] = defaultdict(Decimal)
    for r in usage:
        purposes[r.purpose] += r.cost or Decimal(0)

    tool_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "failed": 0, "denied": 0})
    for t in inp.tools:
        st = tool_stats[t.tool]
        st["calls"] += 1
        st["failed"] += t.status == "failed"
        st["denied"] += t.status == "denied"

    retry_cost = _sum([r for r in usage if r.attempt_id in failed_attempt_ids])
    failed_run_cost = _sum([r for r in usage if r.run_id and run_status.get(r.run_id) in FAILED_RUN])

    # daily trend (zero-filled over the window)
    start_day = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    daily: dict[str, dict[str, Any]] = {}
    for i in range(days):
        d = (start_day + timedelta(days=i)).date().isoformat()
        daily[d] = {"day": d, "calls": 0, "estimated_cost_usd": 0.0}
    for r in usage:
        d = r.at.astimezone(start_day.tzinfo).date().isoformat()
        if d in daily:
            daily[d]["calls"] += 1
            daily[d]["estimated_cost_usd"] += float(r.cost or 0)
    series = list(daily.values())
    trend: dict[str, Any] | None = None
    if days >= 14:
        last7 = sum(d["estimated_cost_usd"] for d in series[-7:])
        prev7 = sum(d["estimated_cost_usd"] for d in series[-14:-7])
        trend = {
            "last_7_days_usd": last7,
            "previous_7_days_usd": prev7,
            "change": ((last7 - prev7) / prev7) if prev7 else None,
        }

    sims = sum(1 for r in usage if r.basis == "simulated")
    return {
        "label": ESTIMATE_LABEL,
        "window_days": days,
        "generated_at": now.isoformat(),
        "totals": {
            "calls": len(usage),
            "estimated_cost_usd": _f(total),
            "calls_with_unknown_cost": unknown,
            "simulated_share": _ratio(sims, len(usage)),
            "retry_cost_usd": _f(retry_cost),
            "retry_cost_share": _ratio(float(retry_cost), float(total)),
            "failed_run_cost_usd": _f(failed_run_cost),
            "failed_run_cost_share": _ratio(float(failed_run_cost), float(total)),
            "pricing_versions": sorted({r.pricing_version for r in usage}),
        },
        "by_agent": by_agent,
        "by_workflow": by_workflow,
        "by_model": by_model,
        "by_purpose": {k: float(v) for k, v in sorted(purposes.items())},
        "tools": [{"tool": k, **v} for k, v in sorted(tool_stats.items(), key=lambda kv: -kv[1]["calls"])],
        "daily": series,
        "trend": trend,
        "median_call_cost_usd": _f(statistics.median([r.cost for r in usage if r.cost is not None]))
        if any(r.cost is not None for r in usage)
        else None,
        "truncated": inp.truncated,
        "notes": [
            "Estimates from reported token usage and the versioned price table; never invoice amounts.",
            "Calls whose provider reported no usage have unknown cost and are excluded from sums, not treated as zero.",
        ],
    }


async def load_inputs(session: AsyncSession, workspace_id: uuid.UUID, *, since: datetime) -> Inputs:
    ws = func.coalesce(ModelUsage.workspace_id, WorkflowRun.workspace_id)
    rows = (
        await session.execute(
            select(
                ModelUsage,
                func.coalesce(AgentDefinition.name, "(no agent)"),
                func.coalesce(WorkflowDefinition.name, "(autopilot)"),
            )
            .select_from(ModelUsage)
            .outerjoin(WorkflowRun, WorkflowRun.id == ModelUsage.run_id)
            .outerjoin(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
            .outerjoin(AgentDefinition, AgentDefinition.id == ModelUsage.agent_definition_id)
            .where(ws == workspace_id, ModelUsage.recorded_at >= since)
            .order_by(ModelUsage.recorded_at)
            .limit(MAX_ROWS + 1)
        )
    ).all()
    inp = Inputs(truncated=len(rows) > MAX_ROWS)
    for u, agent, wf in rows[:MAX_ROWS]:
        inp.usage.append(
            UsageRow(
                u.recorded_at,
                u.run_id,
                u.attempt_id,
                u.agent_definition_id,
                agent,
                wf,
                u.provider,
                u.model,
                u.input_tokens,
                u.output_tokens,
                u.latency_ms,
                u.estimated_cost_usd,
                u.cost_basis,
                u.pricing_version,
                u.status,
                u.purpose,
            )
        )
    run_rows = (
        await session.execute(
            select(WorkflowRun.id, WorkflowDefinition.name, WorkflowRun.status, WorkflowRun.created_at)
            .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
            .where(WorkflowRun.workspace_id == workspace_id, WorkflowRun.created_at >= since)
        )
    ).all()
    inp.runs = [RunRow(*r) for r in run_rows]
    step_rows = (
        await session.execute(
            select(
                WorkflowStep.id, WorkflowStep.run_id, WorkflowStep.agent_definition_id, WorkflowStep.status
            )
            .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
            .where(
                WorkflowRun.workspace_id == workspace_id,
                WorkflowRun.created_at >= since,
                WorkflowStep.agent_definition_id.is_not(None),
            )
        )
    ).all()
    inp.steps = [StepRow(*r) for r in step_rows]
    att_rows = (
        await session.execute(
            select(
                StepAttempt.id,
                StepAttempt.run_id,
                WorkflowStep.agent_definition_id,
                StepAttempt.status,
                StepAttempt.started_at,
                StepAttempt.queue_wait_ms,
            )
            .join(WorkflowStep, WorkflowStep.id == StepAttempt.step_id)
            .join(WorkflowRun, WorkflowRun.id == StepAttempt.run_id)
            .where(WorkflowRun.workspace_id == workspace_id, StepAttempt.started_at >= since)
        )
    ).all()
    inp.attempts = [AttemptRow(*r) for r in att_rows]
    from agentos_core.tools.registry import get_registry

    reg = get_registry()
    tool_rows = (
        await session.execute(
            select(
                ToolExecution.run_id,
                WorkflowStep.agent_definition_id,
                ToolExecution.tool_name,
                ToolExecution.status,
                ToolExecution.arguments_hash,
            )
            .join(WorkflowStep, WorkflowStep.id == ToolExecution.step_id)
            .join(WorkflowRun, WorkflowRun.id == ToolExecution.run_id)
            .where(WorkflowRun.workspace_id == workspace_id, ToolExecution.created_at >= since)
        )
    ).all()
    inp.tools = [
        ToolRow(r, a, t, st, h, read_only=(reg.get(t) is None or reg.get(t).permission == "read"))  # type: ignore[union-attr]
        for r, a, t, st, h in tool_rows
    ]
    return inp


async def summary(
    session: AsyncSession, workspace_id: uuid.UUID, *, days: int = 30, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    inp = await load_inputs(session, workspace_id, since=now - timedelta(days=days))
    return summarize(inp, now=now, days=days)
