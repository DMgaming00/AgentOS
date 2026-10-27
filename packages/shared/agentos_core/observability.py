"""Platform observability views computed from stored records (no sampling, no synthetic data).

* ``telemetry``: workflow duration, queue latency, database latency (a live probe, labeled as such), model-call and tool
  latency distributions, retry and failure rates, tokens, estimated cost, approval wait, recovery duration, worker
  utilization, an agent x error failure heatmap, the agent -> tool dependency graph and evaluation summaries.
* ``workers_view``: each worker's heartbeat, utilization and recent attempts.
* ``run_inspector``: everything stored about one run, correlated by the same run id and trace id: a span waterfall (run, step,
  attempt, model call, tool call, approval), tool activity, cost, pinned versions with deployments and releases, evaluation
  linkage, failure signals and diagnoses, candidates for the agents involved, and an event-integrity check.
"""

from __future__ import annotations

import itertools
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    AgentDeployment,
    ApprovalRequest,
    Artifact,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    Event,
    FailureDiagnosis,
    FailureSignal,
    ModelUsage,
    OptimizationCandidate,
    ReleasePlan,
    StepAttempt,
    ToolExecution,
    Worker,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.finops import ESTIMATE_LABEL
from agentos_core.util import utcnow
from openeval.metrics import nearest_rank

SECONDS_EDGES = [0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 300]
MS_EDGES = [10, 50, 100, 250, 500, 1000, 2500, 5000, 10000]
MAX_SAMPLES = 50_000


def histogram(values: Sequence[float], edges: Sequence[float]) -> list[dict[str, Any]]:
    """Counts per bucket: ``le`` is the bucket's upper bound (the last bucket is open-ended)."""
    counts = [0] * (len(edges) + 1)
    for v in values:
        for i, e in enumerate(edges):
            if v <= e:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    return [{"le": e, "count": c} for e, c in zip(edges, counts, strict=False)] + [
        {"le": None, "count": counts[-1]}
    ]


def dist(values: Sequence[float], edges: Sequence[float]) -> dict[str, Any]:
    values = list(values)
    return {
        "n": len(values),
        "p50": nearest_rank(values, 0.5),
        "p95": nearest_rank(values, 0.95),
        "p99": nearest_rank(values, 0.99),
        "max": max(values) if values else None,
        "histogram": histogram(values, edges),
    }


def _ms(a: datetime | None, b: datetime | None) -> float | None:
    return (b - a).total_seconds() * 1000 if a and b else None


async def db_probe(session: AsyncSession, n: int = 15) -> dict[str, Any]:
    """Round-trip latency of trivial and indexed queries right now. A probe of this API process's connection, not a
    distribution of the platform's real query workload."""
    simple: list[float] = []
    indexed: list[float] = []
    for _ in range(n):
        t = time.perf_counter()
        await session.execute(text("SELECT 1"))
        simple.append((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        await session.execute(select(WorkflowRun.id).limit(1))
        indexed.append((time.perf_counter() - t) * 1000)
    return {
        "kind": "probe",
        "note": "live round-trip probe from the API process; not the distribution of workload queries",
        "select_1_ms": {
            "p50": nearest_rank(simple, 0.5),
            "p95": nearest_rank(simple, 0.95),
            "max": max(simple),
        },
        "indexed_read_ms": {
            "p50": nearest_rank(indexed, 0.5),
            "p95": nearest_rank(indexed, 0.95),
            "max": max(indexed),
        },
        "samples": n,
    }


async def telemetry(
    session: AsyncSession, workspace_id: uuid.UUID, *, days: int = 7, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    since = now - timedelta(days=days)
    run_scope = (WorkflowRun.workspace_id == workspace_id, WorkflowRun.created_at >= since)

    runs = (
        await session.execute(
            select(WorkflowRun.status, WorkflowRun.started_at, WorkflowRun.completed_at, WorkflowRun.purpose)
            .where(*run_scope)
            .limit(MAX_SAMPLES)
        )
    ).all()
    terminal = [r for r in runs if r[0] in ("succeeded", "failed", "cancelled", "timed_out")]
    durations = [(c - s).total_seconds() for _, s, c, p in terminal if s and c and p == "workflow"]
    failed_runs = sum(1 for r in terminal if r[0] in ("failed", "timed_out"))

    attempts = (
        await session.execute(
            select(
                StepAttempt.status,
                StepAttempt.queue_wait_ms,
                StepAttempt.step_id,
                StepAttempt.started_at,
                StepAttempt.finished_at,
                StepAttempt.error_type,
                WorkflowStep.agent_definition_id,
            )
            .join(WorkflowStep, WorkflowStep.id == StepAttempt.step_id)
            .join(WorkflowRun, WorkflowRun.id == StepAttempt.run_id)
            .where(*run_scope)
            .order_by(StepAttempt.step_id, StepAttempt.attempt_number)
            .limit(MAX_SAMPLES)
        )
    ).all()
    queue = [float(a[1]) for a in attempts if a[1] is not None]
    retried = sum(1 for a in attempts if a[0] in ("failed", "lease_expired"))
    # recovery: a lease-expired attempt, then the next attempt of the same step starts
    recovery: list[float] = []
    by_step: dict[uuid.UUID, list[Any]] = defaultdict(list)
    for a in attempts:
        by_step[a[2]].append(a)
    for rows in by_step.values():
        for prev, nxt in itertools.pairwise(rows):
            if prev[0] == "lease_expired" and prev[4] and nxt[3]:
                recovery.append(max(0.0, (nxt[3] - prev[4]).total_seconds() * 1000))

    usage = (
        await session.execute(
            select(
                ModelUsage.provider,
                ModelUsage.model,
                ModelUsage.latency_ms,
                ModelUsage.input_tokens,
                ModelUsage.output_tokens,
                ModelUsage.estimated_cost_usd,
                ModelUsage.status,
            )
            .join(WorkflowRun, WorkflowRun.id == ModelUsage.run_id)
            .where(*run_scope)
            .limit(MAX_SAMPLES)
        )
    ).all()
    by_model: dict[str, list[float]] = defaultdict(list)
    for p, m, lat, *_ in usage:
        by_model[f"{p}/{m}"].append(float(lat))

    tools = (
        await session.execute(
            select(
                ToolExecution.tool_name,
                ToolExecution.status,
                ToolExecution.started_at,
                ToolExecution.completed_at,
                WorkflowStep.agent_definition_id,
            )
            .join(WorkflowStep, WorkflowStep.id == ToolExecution.step_id)
            .join(WorkflowRun, WorkflowRun.id == ToolExecution.run_id)
            .where(*run_scope)
            .limit(MAX_SAMPLES)
        )
    ).all()
    tool_lat: dict[str, list[float]] = defaultdict(list)
    for name, st, started, finished, _ in tools:
        ms = _ms(started, finished)
        if st == "succeeded" and ms is not None:
            tool_lat[name].append(ms)

    approvals = (
        await session.execute(
            select(ApprovalRequest.status, ApprovalRequest.requested_at, ApprovalRequest.decided_at).where(
                ApprovalRequest.workspace_id == workspace_id, ApprovalRequest.requested_at >= since
            )
        )
    ).all()
    waits = [(d - r).total_seconds() for st, r, d in approvals if d and st in ("approved", "rejected")]
    pending = [r for st, r, d in approvals if st == "pending"]

    names = dict(
        (
            await session.execute(
                select(AgentDefinition.id, AgentDefinition.name).where(
                    AgentDefinition.workspace_id == workspace_id
                )
            )
        ).all()
    )
    heat: dict[tuple[str, str], int] = Counter()
    for a in attempts:
        if a[5] and a[6]:
            heat[(names.get(a[6], "?"), a[5])] += 1
    edges: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: {"calls": 0, "failed": 0})
    for name, st, _, _, agent_id in tools:
        if agent_id:
            e = edges[(names.get(agent_id, "?"), name)]
            e["calls"] += 1
            e["failed"] += st in ("failed", "denied")

    workers = (await session.execute(select(Worker))).scalars().all()
    live = [w for w in workers if w.status == "active" and (now - w.last_heartbeat_at).total_seconds() < 30]
    capacity = sum(w.concurrency for w in live)

    evals = (
        await session.execute(
            select(EvaluationRun, AgentDefinition.name, EvaluationDataset.name, EvaluationDataset.split)
            .join(AgentDefinition, AgentDefinition.id == EvaluationRun.agent_id)
            .join(EvaluationDataset, EvaluationDataset.id == EvaluationRun.dataset_id)
            .where(EvaluationRun.workspace_id == workspace_id, EvaluationRun.status == "completed")
            .order_by(EvaluationRun.completed_at.desc())
            .limit(12)
        )
    ).all()

    tokens_in = sum(u[3] or 0 for u in usage)
    tokens_out = sum(u[4] or 0 for u in usage)
    cost = sum((u[5] for u in usage if u[5] is not None), start=0)
    finished_steps = Counter(a[0] for a in attempts)
    return {
        "window_days": days,
        "generated_at": now.isoformat(),
        "workflow_duration_s": dist(durations, SECONDS_EDGES),
        "queue_latency_ms": dist(queue, MS_EDGES),
        "database_latency": await db_probe(session),
        "model_latency_ms": {
            "overall": dist([v for vs in by_model.values() for v in vs], MS_EDGES),
            "by_model": {k: dist(v, MS_EDGES) for k, v in sorted(by_model.items())},
        },
        "tool_latency_ms": {k: dist(v, MS_EDGES) for k, v in sorted(tool_lat.items())},
        "rates": {
            "attempts": len(attempts),
            "retry_rate": (retried / len(attempts)) if attempts else None,
            "terminal_runs": len(terminal),
            "run_failure_rate": (failed_runs / len(terminal)) if terminal else None,
            "attempt_status": dict(finished_steps),
        },
        "tokens": {
            "input": tokens_in,
            "output": tokens_out,
            "calls": len(usage),
            "calls_missing_usage": sum(1 for u in usage if u[3] is None),
        },
        "estimated_cost": {"usd": float(cost), "label": ESTIMATE_LABEL},
        "approval_wait_s": {
            **dist(waits, SECONDS_EDGES),
            "pending": len(pending),
            "oldest_pending_s": max(((now - r).total_seconds() for r in pending), default=None),
        },
        "recovery_ms": dist(recovery, MS_EDGES),
        "workers": {
            "live": len(live),
            "total_seen": len(workers),
            "capacity": capacity,
            "in_flight": sum(w.in_flight for w in live),
            "utilization": (sum(w.in_flight for w in live) / capacity) if capacity else None,
        },
        "failure_heatmap": [
            {"agent": a, "error": e, "count": n} for (a, e), n in sorted(heat.items(), key=lambda kv: -kv[1])
        ],
        "dependency_graph": [{"agent": a, "tool": t, **v} for (a, t), v in sorted(edges.items())],
        "evaluations": [
            {
                "id": str(e.id),
                "agent": agent,
                "dataset": f"{ds}/{split}",
                "success": ((e.metrics or {}).get("task_success_rate") or {}).get("value"),
                "cases": (e.metrics or {}).get("cases"),
                "completed_at": e.completed_at.isoformat() if e.completed_at else None,
            }
            for e, agent, ds, split in evals
        ],
    }


async def workers_view(
    session: AsyncSession, *, now: datetime | None = None, window_minutes: int = 60
) -> dict[str, Any]:
    now = now or utcnow()
    since = now - timedelta(minutes=window_minutes)
    workers = (
        (await session.execute(select(Worker).order_by(Worker.last_heartbeat_at.desc()).limit(50)))
        .scalars()
        .all()
    )
    stats = {
        wid: (n, ok, bad, mean_q)
        for wid, n, ok, bad, mean_q in (
            await session.execute(
                select(
                    StepAttempt.worker_id,
                    func.count(),
                    func.sum(case((StepAttempt.status == "succeeded", 1), else_=0)),
                    func.sum(case((StepAttempt.status.in_(["failed", "lease_expired"]), 1), else_=0)),
                    func.avg(StepAttempt.queue_wait_ms),
                )
                .where(StepAttempt.started_at >= since)
                .group_by(StepAttempt.worker_id)
            )
        ).all()
    }
    items = []
    for w in workers:
        age = (now - w.last_heartbeat_at).total_seconds()
        n, ok, bad, mean_q = stats.get(w.id, (0, 0, 0, None))
        items.append(
            {
                "id": w.id,
                "hostname": w.hostname,
                "pid": w.pid,
                "status": w.status,
                "concurrency": w.concurrency,
                "in_flight": w.in_flight,
                "utilization": (w.in_flight / w.concurrency) if w.concurrency else None,
                "steps_completed": w.steps_completed,
                "started_at": w.started_at.isoformat(),
                "last_heartbeat_at": w.last_heartbeat_at.isoformat(),
                "heartbeat_age_s": round(age, 1),
                "healthy": w.status == "active" and age < 30,
                "recent": {
                    "window_minutes": window_minutes,
                    "attempts": int(n or 0),
                    "succeeded": int(ok or 0),
                    "failed": int(bad or 0),
                    "mean_queue_wait_ms": float(mean_q) if mean_q is not None else None,
                },
            }
        )
    live = [i for i in items if i["healthy"]]
    return {
        "items": items,
        "summary": {
            "healthy": len(live),
            "total": len(items),
            "capacity": sum(i["concurrency"] for i in live),
            "in_flight": sum(i["in_flight"] for i in live),
        },
        "note": "Heartbeat age under 30 s counts as healthy. Utilization is in-flight steps over configured concurrency at the last heartbeat.",
    }


def span(
    kind: str,
    id_: Any,
    parent: Any,
    label: str,
    start: datetime | None,
    end: datetime | None,
    status: str | None,
    **details: Any,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": str(id_),
        "parent": str(parent) if parent else None,
        "label": label,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "duration_ms": _ms(start, end),
        "status": status,
        "details": details,
    }


async def run_inspector(session: AsyncSession, run: WorkflowRun) -> dict[str, Any]:
    wf = await session.get(WorkflowDefinition, run.workflow_definition_id)
    steps = list(
        (
            await session.execute(
                select(WorkflowStep).where(WorkflowStep.run_id == run.id).order_by(WorkflowStep.created_at)
            )
        ).scalars()
    )
    attempts = list(
        (
            await session.execute(
                select(StepAttempt).where(StepAttempt.run_id == run.id).order_by(StepAttempt.started_at)
            )
        ).scalars()
    )
    tools = list(
        (
            await session.execute(
                select(ToolExecution).where(ToolExecution.run_id == run.id).order_by(ToolExecution.created_at)
            )
        ).scalars()
    )
    usage = list(
        (
            await session.execute(
                select(ModelUsage).where(ModelUsage.run_id == run.id).order_by(ModelUsage.recorded_at)
            )
        ).scalars()
    )
    approvals = list(
        (await session.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == run.id))).scalars()
    )
    events = list(
        (
            await session.execute(
                select(Event.sequence_number, Event.span_id, Event.trace_id)
                .where(Event.run_id == run.id)
                .order_by(Event.sequence_number)
            )
        ).all()
    )
    names = (
        dict((await session.execute(select(AgentDefinition.id, AgentDefinition.name))).all()) if steps else {}
    )

    spans = [
        span(
            "run",
            run.id,
            None,
            wf.name if wf else "run",
            run.started_at or run.created_at,
            run.completed_at,
            run.status,
            trace_id=run.trace_id,
            environment=run.environment,
            purpose=run.purpose,
        )
    ]
    for s in steps:
        spans.append(
            span(
                "step",
                s.id,
                run.id,
                s.node_key,
                s.started_at,
                s.completed_at,
                s.status,
                node_type=s.node_type,
                agent=names.get(s.agent_definition_id) if s.agent_definition_id else None,
                version_id=str(s.agent_version_id) if s.agent_version_id else None,
            )
        )
    attempt_by_step: dict[uuid.UUID, list[StepAttempt]] = defaultdict(list)
    for a in attempts:
        attempt_by_step[a.step_id].append(a)
        spans.append(
            span(
                "attempt",
                a.id,
                a.step_id,
                f"attempt {a.attempt_number}",
                a.started_at,
                a.finished_at,
                a.status,
                worker=a.worker_id,
                queue_wait_ms=a.queue_wait_ms,
                error=a.error_type,
            )
        )

    def parent_attempt(step_id: uuid.UUID | None, at: datetime | None) -> uuid.UUID | None:
        rows = attempt_by_step.get(step_id, []) if step_id else []
        for a in reversed(rows):
            if at is None or a.started_at <= at:
                return a.id
        return step_id

    for u in usage:
        start = u.recorded_at - timedelta(milliseconds=u.latency_ms)
        spans.append(
            span(
                "model_call",
                u.id,
                parent_attempt(u.step_id, start),
                f"{u.provider}/{u.model}",
                start,
                u.recorded_at,
                u.status,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cost_usd=float(u.estimated_cost_usd) if u.estimated_cost_usd is not None else None,
                cost_basis=u.cost_basis,
                pricing_version=u.pricing_version,
            )
        )
    for t in tools:
        spans.append(
            span(
                "tool_call",
                t.id,
                parent_attempt(t.step_id, t.started_at or t.created_at),
                t.tool_name,
                t.started_at or t.created_at,
                t.completed_at,
                t.status,
                arguments_hash=t.arguments_hash[:12],
                requires_approval=t.requires_approval,
                error=(t.error or {}).get("type"),
            )
        )
    for ap in approvals:
        spans.append(
            span(
                "approval",
                ap.id,
                ap.step_id,
                ap.requested_action,
                ap.requested_at,
                ap.decided_at,
                ap.status,
                tool_execution_id=str(ap.tool_execution_id) if ap.tool_execution_id else None,
            )
        )

    total_cost = sum((float(u.estimated_cost_usd) for u in usage if u.estimated_cost_usd is not None), 0.0)
    by_step: dict[uuid.UUID | None, float] = defaultdict(float)
    by_model: dict[str, dict[str, float]] = defaultdict(lambda: {"calls": 0, "cost": 0.0, "in": 0, "out": 0})
    for u in usage:
        c = float(u.estimated_cost_usd) if u.estimated_cost_usd is not None else 0.0
        by_step[u.step_id] += c
        m = by_model[f"{u.provider}/{u.model}"]
        m["calls"] += 1
        m["cost"] += c
        m["in"] += u.input_tokens or 0
        m["out"] += u.output_tokens or 0
    failed_attempts = {a.id for a in attempts if a.status in ("failed", "lease_expired")}
    retry_cost = sum(
        (
            float(u.estimated_cost_usd)
            for u in usage
            if u.attempt_id in failed_attempts and u.estimated_cost_usd is not None
        ),
        0.0,
    )

    version_ids = {s.agent_version_id for s in steps if s.agent_version_id}
    versions = []
    for vid in version_ids:
        v = await session.get(AgentConfigVersion, vid)
        agent = await session.get(AgentDefinition, v.agent_id) if v else None
        if v is None or agent is None:
            continue
        role = (
            "active"
            if agent.active_version_id == v.id
            else "canary"
            if agent.canary_version_id == v.id
            else "historical"
        )
        deps = (
            (
                await session.execute(
                    select(AgentDeployment)
                    .where(AgentDeployment.config_version_id == v.id)
                    .order_by(AgentDeployment.deployed_at)
                )
            )
            .scalars()
            .all()
        )
        plans = (
            (
                await session.execute(
                    select(ReleasePlan)
                    .where(
                        (ReleasePlan.candidate_version_id == v.id) | (ReleasePlan.baseline_version_id == v.id)
                    )
                    .order_by(ReleasePlan.created_at.desc())
                    .limit(3)
                )
            )
            .scalars()
            .all()
        )
        versions.append(
            {
                "agent": agent.name,
                "agent_id": str(agent.id),
                "version": v.version,
                "version_id": str(v.id),
                "config_hash": v.config_hash[:16],
                "source": v.source,
                "role_now": role,
                "pinned_environment": run.environment,
                "deployments": [
                    {
                        "id": str(d.id),
                        "type": d.deployment_type,
                        "status": d.status,
                        "canary_percent": d.canary_percent,
                        "deployed_at": d.deployed_at.isoformat(),
                        "approved_by": str(d.approved_by) if d.approved_by else None,
                    }
                    for d in deps
                ],
                "releases": [
                    {
                        "id": str(p.id),
                        "environment": p.environment,
                        "status": p.status,
                        "role": "candidate" if p.candidate_version_id == v.id else "baseline",
                    }
                    for p in plans
                ],
            }
        )

    agent_ids = {s.agent_definition_id for s in steps if s.agent_definition_id}
    evals = (
        await session.execute(
            select(
                EvaluationCaseResult,
                EvaluationCase.case_key,
                EvaluationRun.id,
                EvaluationDataset.name,
                EvaluationDataset.split,
            )
            .join(EvaluationCase, EvaluationCase.id == EvaluationCaseResult.case_id)
            .join(EvaluationRun, EvaluationRun.id == EvaluationCaseResult.evaluation_run_id)
            .join(EvaluationDataset, EvaluationDataset.id == EvaluationRun.dataset_id)
            .where(EvaluationCaseResult.workflow_run_id == run.id)
        )
    ).all()
    signals = list(
        (
            await session.execute(
                select(FailureSignal)
                .where(FailureSignal.run_id == run.id)
                .order_by(FailureSignal.occurred_at)
            )
        ).scalars()
    )
    diagnoses = []
    if agent_ids and signals:
        cats = {(s.agent_id, s.category) for s in signals}
        for agent_id, cat in cats:
            for d in (
                await session.execute(
                    select(FailureDiagnosis)
                    .where(FailureDiagnosis.agent_id == agent_id, FailureDiagnosis.failure_category == cat)
                    .order_by(FailureDiagnosis.created_at.desc())
                    .limit(1)
                )
            ).scalars():
                diagnoses.append(
                    {
                        "id": str(d.id),
                        "agent_id": str(d.agent_id),
                        "category": d.failure_category,
                        "hypothesis": d.hypothesis,
                        "confidence": d.confidence,
                        "status": d.status,
                    }
                )
    candidates = []
    if agent_ids:
        for cand in (
            await session.execute(
                select(OptimizationCandidate)
                .where(OptimizationCandidate.agent_id.in_(agent_ids))
                .order_by(OptimizationCandidate.created_at.desc())
                .limit(6)
            )
        ).scalars():
            candidates.append(
                {
                    "id": str(cand.id),
                    "agent_id": str(cand.agent_id),
                    "title": cand.title,
                    "status": cand.status,
                    "generator": cand.generator,
                    "rule_id": cand.rule_id,
                    "candidate_version_id": str(cand.candidate_version_id)
                    if cand.candidate_version_id
                    else None,
                }
            )

    seqs = [e[0] for e in events]
    gap_free = bool(seqs) and seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    artifacts = (
        await session.execute(select(func.count()).select_from(Artifact).where(Artifact.run_id == run.id))
    ).scalar_one()
    return {
        "run": {
            "id": str(run.id),
            "status": run.status,
            "workflow": wf.name if wf else None,
            "workflow_version": run.workflow_version,
            "environment": run.environment,
            "purpose": run.purpose,
            "trace_id": run.trace_id,
            "created_at": run.created_at.isoformat(),
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
        },
        "correlation": {
            "trace_id": run.trace_id,
            "span_ids_in_events": len({e[1] for e in events if e[1]}),
            "note": "The trace id links this run to the HTTP request that created it and to spans exported to an OpenTelemetry backend; "
            "this view is built from the records stored in PostgreSQL, joined by run id.",
        },
        "spans": spans,
        "tools": [
            {
                "id": str(t.id),
                "tool": t.tool_name,
                "status": t.status,
                "duration_ms": _ms(t.started_at, t.completed_at),
                "requires_approval": t.requires_approval,
                "error": (t.error or {}).get("type"),
                "arguments_hash": t.arguments_hash[:12],
                "step_id": str(t.step_id),
            }
            for t in tools
        ],
        "cost": {
            "label": ESTIMATE_LABEL,
            "total_usd": total_cost,
            "retry_cost_usd": retry_cost,
            "run_reported_usd": float(run.cost_spent_usd),
            "by_step": [
                {
                    "step_id": str(k) if k else None,
                    "node_key": next((s.node_key for s in steps if s.id == k), None),
                    "usd": v,
                }
                for k, v in by_step.items()
            ],
            "by_model": [{"model": k, **v} for k, v in by_model.items()],
        },
        "versions": versions,
        "evaluation": [
            {
                "evaluation_run_id": str(rid),
                "dataset": f"{ds}/{split}",
                "case_key": key,
                "passed": r.passed,
                "status": r.status,
                "error": r.error,
                "scores": {m: {"passed": v.get("passed")} for m, v in (r.scores or {}).items()},
            }
            for r, key, rid, ds, split in evals
        ],
        "failure": {
            "signals": [
                {
                    "id": str(s.id),
                    "category": s.category,
                    "detector": s.detector,
                    "summary": s.summary,
                    "step_id": str(s.step_id) if s.step_id else None,
                    "infrastructure": s.is_infrastructure,
                }
                for s in signals
            ],
            "diagnoses": diagnoses,
        },
        "candidates": candidates,
        "integrity": {
            "events": len(seqs),
            "first_sequence": seqs[0] if seqs else None,
            "last_sequence": seqs[-1] if seqs else None,
            "gap_free": gap_free,
            "monotonic": seqs == sorted(seqs),
        },
        "artifacts": artifacts,
        "links": {"flight_recorder": f"/flight/{run.id}", "live_graph": f"/runs/{run.id}"},
    }
