"""Failure Intelligence: deterministic detectors over persisted execution traces.

Every signal links to the evidence it came from (events, steps, attempts, tool
executions, model calls, evaluation results) and records the measurements and
thresholds used. Signals are fingerprinted, so re-running analysis is idempotent.
"""

from __future__ import annotations

import itertools
import statistics
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.taxonomy import (
    EVALUATOR_MAP,
    FailureCategory,
    classify_error_type,
    is_infrastructure,
)
from agentos_core.db.models import (
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    Event,
    FailureSignal,
    ModelUsage,
    StepAttempt,
    ToolExecution,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.redaction import redact
from agentos_core.util import sha256_hex, utcnow


@dataclass(frozen=True)
class Thresholds:
    repeated_tool_calls: int = 2  # identical calls per step beyond this are a loop
    non_progress_turns: int = 3  # identical consecutive model turns
    excessive_retries: int = 2  # failures on one step
    cost_spike_factor: float = 3.0  # x agent median step cost
    cost_spike_min_usd: float = 0.001
    latency_factor: float = 2.0  # x agent p95 step duration
    min_history: int = 5  # samples required before anomaly baselines are trusted


@dataclass
class Signal:
    category: FailureCategory
    signal_type: str
    detector: str
    run_id: uuid.UUID
    step_id: uuid.UUID | None
    agent_id: uuid.UUID | None
    agent_version_id: uuid.UUID | None
    summary: str
    evidence: list[dict[str, Any]]
    occurred_at: datetime
    measurements: dict[str, Any] = field(default_factory=dict)
    cost_usd: Decimal | None = None
    key: str = ""

    def fingerprint(self) -> str:
        return sha256_hex(f"{self.detector}:{self.key}")


def ref(kind: str, ident: object, **extra: Any) -> dict[str, Any]:
    """Evidence reference. ``id`` strings like 'event:42' are what diagnoses must cite."""
    return {"id": f"{kind}:{ident}", "kind": kind, **extra}


async def _step_costs(session: AsyncSession, step_ids: list[uuid.UUID]) -> dict[uuid.UUID, Decimal]:
    if not step_ids:
        return {}
    rows = await session.execute(
        select(ModelUsage.step_id, func.coalesce(func.sum(ModelUsage.estimated_cost_usd), 0))
        .where(ModelUsage.step_id.in_(step_ids))
        .group_by(ModelUsage.step_id)
    )
    return {sid: Decimal(str(c)) for sid, c in rows.all() if sid is not None}


async def _step_events(session: AsyncSession, step_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[Event]]:
    out: dict[uuid.UUID, list[Event]] = defaultdict(list)
    if not step_ids:
        return out
    rows = (
        await session.execute(
            select(Event).where(Event.step_id.in_(step_ids)).order_by(Event.run_id, Event.sequence_number)
        )
    ).scalars()
    for e in rows:
        out[e.step_id].append(e)  # type: ignore[index]
    return out


async def detect(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    since: datetime,
    agent_id: uuid.UUID | None = None,
    thresholds: Thresholds = Thresholds(),
) -> list[Signal]:
    run_filter = [WorkflowRun.workspace_id == workspace_id, WorkflowRun.created_at >= since]
    step_q = (
        select(WorkflowStep, WorkflowRun.purpose)
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
        .where(*run_filter)
    )
    if agent_id:
        step_q = step_q.where(WorkflowStep.agent_definition_id == agent_id)
    step_rows = (await session.execute(step_q)).all()
    steps = {s.id: s for s, _ in step_rows}
    ids = list(steps)
    events = await _step_events(session, ids)
    costs = await _step_costs(session, ids)
    signals: list[Signal] = []

    def base(step: WorkflowStep, **kw: Any) -> dict[str, Any]:
        return {
            "run_id": step.run_id,
            "step_id": step.id,
            "agent_id": step.agent_definition_id,
            "agent_version_id": step.agent_version_id,
            **kw,
        }

    # 1. failed steps (terminal)
    for step in steps.values():
        if step.status != "failed":
            continue
        err = step.error or {}
        cat = classify_error_type(err.get("type"), str(err.get("message", "")))
        evs = events[step.id]
        evidence = [ref("step", step.id, node_key=step.node_key)]
        evidence += [
            ref("event", e.id, type=e.event_type, run_id=str(e.run_id))
            for e in evs
            if e.event_type
            in (
                "step.failed",
                "model.call.completed",
                "model.call.failed",
                "tool.failed",
                "tool.denied",
                "budget.exceeded",
            )
        ][-6:]
        signals.append(
            Signal(
                cat,
                "error",
                "failed_step",
                summary=f"Step {step.node_key} failed: {err.get('type')}: "
                f"{str(err.get('message', ''))[:200]}",
                evidence=evidence,
                occurred_at=step.completed_at or step.updated_at,
                cost_usd=costs.get(step.id),
                measurements={"error_type": err.get("type"), "attempts": step.attempt_count},
                key=f"step:{step.id}",
                **base(step),
            )
        )
        # 2. excessive retries
        if step.failure_count > thresholds.excessive_retries:
            signals.append(
                Signal(
                    FailureCategory.EXCESSIVE_RETRIES,
                    "anomaly",
                    "excessive_retries",
                    summary=f"Step {step.node_key} failed {step.failure_count} times",
                    evidence=[ref("step", step.id, node_key=step.node_key)],
                    occurred_at=step.updated_at,
                    cost_usd=costs.get(step.id),
                    measurements={
                        "failure_count": step.failure_count,
                        "threshold": thresholds.excessive_retries,
                    },
                    key=f"retries:{step.id}",
                    **base(step),
                )
            )
    for step in steps.values():
        if step.status != "failed" and step.failure_count > thresholds.excessive_retries:
            signals.append(
                Signal(
                    FailureCategory.EXCESSIVE_RETRIES,
                    "anomaly",
                    "excessive_retries",
                    summary=f"Step {step.node_key} needed {step.failure_count} retries",
                    evidence=[ref("step", step.id, node_key=step.node_key)],
                    occurred_at=step.updated_at,
                    measurements={
                        "failure_count": step.failure_count,
                        "threshold": thresholds.excessive_retries,
                    },
                    key=f"retries:{step.id}",
                    **base(step),
                )
            )

    # 3. transient attempt failures that were retried (infrastructure noise is reported, not blamed on the agent)
    attempts = (
        (
            await session.execute(
                select(StepAttempt).where(
                    StepAttempt.step_id.in_(ids), StepAttempt.status.in_(["failed", "lease_expired"])
                )
            )
        )
        .scalars()
        .all()
        if ids
        else []
    )
    for a in attempts:
        step = steps[a.step_id]
        if step.status == "failed" and a.attempt_number == step.attempt_count:
            continue  # already covered by the failed-step signal
        cat = classify_error_type(a.error_type, a.error_message or "")
        signals.append(
            Signal(
                cat,
                "error",
                "failed_attempt",
                summary=f"Attempt {a.attempt_number} of {step.node_key} failed: {a.error_type}",
                evidence=[ref("attempt", a.id, attempt=a.attempt_number, worker=a.worker_id)],
                occurred_at=a.finished_at or a.started_at,
                measurements={"error_type": a.error_type},
                key=f"attempt:{a.id}",
                **base(step),
            )
        )

    # 4. tool execution failures and denials
    tes = (
        (await session.execute(select(ToolExecution).where(ToolExecution.step_id.in_(ids)))).scalars().all()
        if ids
        else []
    )
    for te in tes:
        if te.status not in ("failed", "denied"):
            continue
        step = steps[te.step_id]
        etype = (te.error or {}).get("type")
        if te.status == "denied":
            cat = (
                FailureCategory.BUDGET_EXCEEDED
                if etype == "budget_exceeded"
                else FailureCategory.TOOL_PERMISSION_DENIED
            )
        elif etype == "invalid_tool_arguments":
            cat = FailureCategory.TOOL_ARGUMENT_ERROR
        else:
            cat = FailureCategory.TOOL_EXECUTION_FAILURE
        signals.append(
            Signal(
                cat,
                "error",
                "tool_failure",
                summary=f"{te.tool_name} {te.status}: {str((te.error or {}).get('message', ''))[:200]}",
                evidence=[ref("tool_execution", te.id, tool=te.tool_name, status=te.status)],
                occurred_at=te.completed_at or te.created_at,
                measurements={"tool": te.tool_name, "error_type": etype},
                key=f"te:{te.id}",
                **base(step),
            )
        )
    # 5. repeated identical tool calls within one step
    groups: dict[tuple[uuid.UUID, str, str], list[ToolExecution]] = defaultdict(list)
    for te in tes:
        groups[(te.step_id, te.tool_name, te.arguments_hash)].append(te)
    for (sid, tool, h), calls in groups.items():
        if len(calls) > thresholds.repeated_tool_calls:
            step = steps[sid]
            signals.append(
                Signal(
                    FailureCategory.REPETITIVE_TOOL_LOOP,
                    "anomaly",
                    "repeated_tool_calls",
                    summary=f"{tool} called {len(calls)} times with identical arguments in {step.node_key}",
                    evidence=[ref("tool_execution", t.id, tool=tool) for t in calls[:6]],
                    occurred_at=max(t.created_at for t in calls),
                    measurements={
                        "count": len(calls),
                        "threshold": thresholds.repeated_tool_calls,
                        "arguments_hash": h,
                    },
                    key=f"repeat:{sid}:{h}",
                    **base(step),
                )
            )
    # 6. non-progress loops: identical consecutive model turns (same text and same tool calls)
    for sid, evs in events.items():
        turns = [e for e in evs if e.event_type == "model.call.completed"]
        run_len, best, best_events = 1, 1, turns[:1]
        for prev, cur in itertools.pairwise(turns):
            same = prev.payload.get("text_preview") == cur.payload.get("text_preview") and [
                c.get("name") for c in prev.payload.get("tool_calls") or []
            ] == [c.get("name") for c in cur.payload.get("tool_calls") or []]
            run_len = run_len + 1 if same else 1
            if run_len > best:
                best, best_events = run_len, [prev, cur]
        if best >= thresholds.non_progress_turns:
            step = steps[sid]
            signals.append(
                Signal(
                    FailureCategory.REPETITIVE_TOOL_LOOP,
                    "anomaly",
                    "non_progress_loop",
                    summary=f"{best} identical consecutive model turns in {step.node_key}",
                    evidence=[ref("event", e.id, type=e.event_type) for e in best_events],
                    occurred_at=best_events[-1].timestamp,
                    measurements={"identical_turns": best, "threshold": thresholds.non_progress_turns},
                    key=f"loop:{sid}",
                    **base(step),
                )
            )

    # 7. model call failures not tied to a failed step (e.g. rate limits that later succeeded)
    mus = (
        (
            await session.execute(
                select(ModelUsage).where(ModelUsage.step_id.in_(ids), ModelUsage.status == "failed")
            )
        )
        .scalars()
        .all()
        if ids
        else []
    )
    for mu in mus:
        step = steps[mu.step_id]  # type: ignore[index]
        cat = classify_error_type(mu.error_type)
        signals.append(
            Signal(
                cat,
                "error",
                "model_call_failure",
                summary=f"{mu.provider}/{mu.model} call failed: {mu.error_type}",
                evidence=[ref("model_usage", mu.id, provider=mu.provider, model=mu.model)],
                occurred_at=mu.recorded_at,
                measurements={"error_type": mu.error_type},
                key=f"mu:{mu.id}",
                **base(step),
            )
        )

    # 8/9. cost and latency anomalies vs the agent's own history (requires enough history)
    by_agent: dict[uuid.UUID, list[WorkflowStep]] = defaultdict(list)
    for s in steps.values():
        if s.agent_definition_id and s.status == "succeeded":
            by_agent[s.agent_definition_id].append(s)
    for _aid, items in by_agent.items():
        if len(items) < thresholds.min_history:
            continue
        cost_vals = [float(costs.get(s.id, 0)) for s in items]
        median_cost = statistics.median(cost_vals)
        durations = {
            s.id: (s.completed_at - s.started_at).total_seconds() * 1000
            for s in items
            if s.completed_at and s.started_at
        }
        p95 = sorted(durations.values())[max(0, int(0.95 * len(durations)) - 1)] if durations else None
        for s in items:
            c = float(costs.get(s.id, 0))
            if (
                median_cost > 0
                and c > thresholds.cost_spike_factor * median_cost
                and c >= thresholds.cost_spike_min_usd
            ):
                signals.append(
                    Signal(
                        FailureCategory.COST_ANOMALY,
                        "anomaly",
                        "cost_spike",
                        summary=f"{s.node_key} cost ${c:.5f} vs agent median ${median_cost:.5f}",
                        evidence=[ref("step", s.id, node_key=s.node_key)],
                        occurred_at=s.completed_at or s.updated_at,
                        cost_usd=Decimal(str(c)),
                        measurements={
                            "cost_usd": c,
                            "median_cost_usd": median_cost,
                            "factor": thresholds.cost_spike_factor,
                            "history": len(items),
                        },
                        key=f"cost:{s.id}",
                        **base(s),
                    )
                )
            d = durations.get(s.id)
            if p95 and d and d > thresholds.latency_factor * p95:
                signals.append(
                    Signal(
                        FailureCategory.LATENCY_ANOMALY,
                        "anomaly",
                        "latency_anomaly",
                        summary=f"{s.node_key} took {d:.0f} ms vs agent p95 {p95:.0f} ms",
                        evidence=[ref("step", s.id, node_key=s.node_key)],
                        occurred_at=s.completed_at or s.updated_at,
                        measurements={
                            "duration_ms": d,
                            "p95_ms": p95,
                            "factor": thresholds.latency_factor,
                            "history": len(durations),
                        },
                        key=f"latency:{s.id}",
                        **base(s),
                    )
                )

    # 10. evaluation failures with output (wrong answers rather than crashes)
    q = (
        select(EvaluationCaseResult, EvaluationRun, EvaluationDataset.split)
        .join(EvaluationRun, EvaluationRun.id == EvaluationCaseResult.evaluation_run_id)
        .join(EvaluationDataset, EvaluationDataset.id == EvaluationRun.dataset_id)
        .where(
            EvaluationRun.workspace_id == workspace_id,
            EvaluationCaseResult.passed.is_(False),
            EvaluationCaseResult.observed_output.is_not(None),
            EvaluationCaseResult.graded_at >= since,
        )
    )
    if agent_id:
        q = q.where(EvaluationRun.agent_id == agent_id)
    for res, ev, split in (await session.execute(q)).all():
        failing = [m for m, v in (res.scores or {}).items() if not v.get("passed") and not v.get("advisory")]
        cat = EVALUATOR_MAP.get(failing[0], FailureCategory.UNKNOWN) if failing else FailureCategory.UNKNOWN
        hidden = split != "development"
        # Hidden-split details (which contain expected answers) are never copied into signals.
        details: dict[str, Any] = {"failing_checks": failing, "split": split}
        if not hidden:
            details["failures"] = {
                m: (res.scores or {})[m]["details"].get("failures", [])[:3] for m in failing
            }
        signals.append(
            Signal(
                FailureCategory(cat),
                "evaluation",
                "evaluation_failure",
                run_id=res.workflow_run_id,  # type: ignore[arg-type]  # non-null: filtered on observed output
                step_id=None,
                agent_id=ev.agent_id,
                agent_version_id=ev.agent_version_id,
                summary=f"Evaluation case failed checks: {', '.join(failing)} ({split} split)",
                evidence=[ref("evaluation_result", res.id, evaluation_run_id=str(ev.id), split=split)],
                occurred_at=res.graded_at or utcnow(),
                measurements=details,
                key=f"evalres:{res.id}",
            )
        )
    return signals


async def persist(session: AsyncSession, workspace_id: uuid.UUID, signals: list[Signal]) -> int:
    """Insert new signals (fingerprint-deduplicated). Returns how many were new."""
    new = 0
    now = utcnow()
    for sig in signals:
        if sig.run_id is None:
            continue
        res = await session.execute(
            insert(FailureSignal)
            .values(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                agent_id=sig.agent_id,
                agent_version_id=sig.agent_version_id,
                run_id=sig.run_id,
                step_id=sig.step_id,
                category=sig.category.value,
                signal_type=sig.signal_type,
                detector=sig.detector,
                is_infrastructure=is_infrastructure(sig.category),
                summary=sig.summary[:2000],
                evidence={"refs": redact(sig.evidence)},
                measurements=redact(sig.measurements),
                cost_usd=sig.cost_usd,
                fingerprint=sig.fingerprint(),
                detected_at=now,
                occurred_at=sig.occurred_at,
            )
            .on_conflict_do_nothing(constraint="uq_failure_signals_fingerprint")
        )
        new += res.rowcount or 0  # type: ignore[attr-defined]
    return new


async def analyze(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    window_hours: int = 168,
    agent_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    signals = await detect(
        session, workspace_id, since=utcnow() - timedelta(hours=window_hours), agent_id=agent_id
    )
    new = await persist(session, workspace_id, signals)
    by_cat: dict[str, int] = defaultdict(int)
    for s in signals:
        by_cat[s.category.value] += 1
    return {"detected": len(signals), "new": new, "by_category": dict(by_cat), "window_hours": window_hours}


async def clusters(
    session: AsyncSession, workspace_id: uuid.UUID, *, since: datetime
) -> list[dict[str, Any]]:
    """Group signals by agent and category, with counts, cost and recency."""
    rows = (
        await session.execute(
            select(
                FailureSignal.agent_id,
                FailureSignal.category,
                FailureSignal.is_infrastructure,
                func.count(),
                func.count(func.distinct(FailureSignal.run_id)),
                func.coalesce(func.sum(FailureSignal.cost_usd), 0),
                func.min(FailureSignal.occurred_at),
                func.max(FailureSignal.occurred_at),
            )
            .where(FailureSignal.workspace_id == workspace_id, FailureSignal.occurred_at >= since)
            .group_by(FailureSignal.agent_id, FailureSignal.category, FailureSignal.is_infrastructure)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        {
            "agent_id": str(a) if a else None,
            "category": c,
            "is_infrastructure": infra,
            "signals": n,
            "runs": runs,
            "cost_usd": float(cost or 0),
            "first_seen": first.isoformat(),
            "last_seen": last.isoformat(),
        }
        for a, c, infra, n, runs, cost, first, last in rows
    ]
