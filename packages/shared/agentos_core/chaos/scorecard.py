"""Resilience scorecard for a finished (or partial) chaos experiment, computed from persisted records only.

There is deliberately **no single score**: the metrics answer different questions, are not on a common scale, and
any weighting would be arbitrary. Every metric reports its numerator and denominator so it can be audited, and
descriptive metrics (fault exposure, retry amplification) are labeled as such, not as "good" or "bad".
"""

from __future__ import annotations

import statistics
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import (
    Artifact,
    ChaosExperiment,
    ChaosInjection,
    Event,
    StepAttempt,
    ToolExecution,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.domain.states import TERMINAL_RUN, TERMINAL_STEP

METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "task_completion_rate": {
        "definition": "Runs that ended `succeeded` divided by runs planned. A run still unfinished when the experiment "
        "ended counts as not completed.",
        "denominator": "planned runs (trials)",
        "kind": "outcome",
    },
    "fault_exposure": {
        "definition": "Runs that received at least one injected fault divided by planned runs. Descriptive: it says how "
        "much of the workload the fault schedule touched.",
        "denominator": "planned runs",
        "kind": "descriptive",
    },
    "recovery_rate": {
        "definition": "Among runs that received at least one fault, the share that still ended `succeeded`.",
        "denominator": "affected runs",
        "kind": "outcome",
    },
    "retry_amplification": {
        "definition": "Step attempts divided by steps across all runs (1.0 = no retries), plus extra attempts per "
        "injected fault. Descriptive: more attempts is the cost of recovery, not a failure.",
        "denominator": "steps",
        "kind": "descriptive",
    },
    "time_to_detection_ms": {
        "definition": "From an injection to the first platform reaction recorded for that run after it (a retry, "
        "failure, denial, budget stop or fenced rejection). Faults the platform absorbs silently have no reaction "
        "and are excluded from the denominator (and counted separately).",
        "denominator": "injections with a recorded reaction",
        "kind": "latency",
    },
    "time_to_recovery_ms": {
        "definition": "From an injection to the completion of the step it hit, for steps that ended `succeeded`.",
        "denominator": "injections on steps that later succeeded",
        "kind": "latency",
    },
    "duplicate_side_effect_prevention": {
        "definition": "Of injected duplicate deliveries with recorded evidence, how many were neutralized: a repeated "
        "tool call returned the stored result (or the tool has no side effect) and a repeated step completion was "
        "rejected by the lease fence. Also reports duplicate successful executions found in the database (must be 0).",
        "denominator": "duplicate deliveries injected with evidence",
        "kind": "safety",
    },
    "state_consistency": {
        "definition": "Invariant checks per run: event sequence contiguous from 1; terminal run has only terminal "
        "steps; no attempt still `running` on a terminal step; a succeeded run has no failed step; no step holds a "
        "lease while terminal.",
        "denominator": "invariant checks (5 per run)",
        "kind": "safety",
    },
    "budget_compliance": {
        "definition": "Runs whose cost, model calls and tool calls stayed within their own budget.",
        "denominator": "runs",
        "kind": "safety",
    },
    "policy_held_under_hostile_output": {
        "definition": "Runs that received an injected malicious tool result and produced no unauthorized side effect "
        "(no succeeded artifact_writer execution and no artifact). Shows the policy layer held; it does not measure "
        "whether a model is resistant to prompt injection.",
        "denominator": "runs with a malicious tool result",
        "kind": "safety",
    },
}

REACTION_EVENTS = {
    "step.retrying",
    "step.failed",
    "model.call.failed",
    "tool.failed",
    "tool.denied",
    "budget.exceeded",
    "step.stale_result_rejected",
    "run.failed",
}


def rate(num: int, den: int) -> dict[str, Any]:
    return {"value": (num / den) if den else None, "numerator": num, "denominator": den}


def _nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    import math

    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summary(values: list[float]) -> dict[str, Any]:
    return {
        "n": len(values),
        "median": statistics.median(values) if values else None,
        "p95": _nearest_rank(values, 0.95),
        "max": max(values) if values else None,
    }


async def compute_scorecard(session: AsyncSession, experiment: ChaosExperiment) -> dict[str, Any]:
    exp_id = experiment.id
    planned = int(experiment.plan.get("trials", 0))
    runs = list(
        (
            await session.execute(select(WorkflowRun).where(WorkflowRun.chaos_experiment_id == exp_id))
        ).scalars()
    )
    run_ids = [r.id for r in runs]
    steps = (
        list((await session.execute(select(WorkflowStep).where(WorkflowStep.run_id.in_(run_ids)))).scalars())
        if run_ids
        else []
    )
    attempts = (
        list((await session.execute(select(StepAttempt).where(StepAttempt.run_id.in_(run_ids)))).scalars())
        if run_ids
        else []
    )
    injections = list(
        (
            await session.execute(
                select(ChaosInjection)
                .where(ChaosInjection.experiment_id == exp_id)
                .order_by(ChaosInjection.injected_at)
            )
        ).scalars()
    )
    events = (
        list(
            (
                await session.execute(
                    select(Event)
                    .where(Event.run_id.in_(run_ids))
                    .order_by(Event.run_id, Event.sequence_number)
                )
            ).scalars()
        )
        if run_ids
        else []
    )
    tools = (
        list(
            (await session.execute(select(ToolExecution).where(ToolExecution.run_id.in_(run_ids)))).scalars()
        )
        if run_ids
        else []
    )
    artifacts = (
        list((await session.execute(select(Artifact).where(Artifact.run_id.in_(run_ids)))).scalars())
        if run_ids
        else []
    )

    by_run_steps: dict[uuid.UUID, list[WorkflowStep]] = {}
    for st in steps:
        by_run_steps.setdefault(st.run_id, []).append(st)
    by_run_events: dict[uuid.UUID, list[Event]] = {}
    for e in events:
        by_run_events.setdefault(e.run_id, []).append(e)
    affected_cases = {i.case_index for i in injections}
    case_of = {r.id: r.chaos_case_index for r in runs}

    succeeded = [r for r in runs if r.status == "succeeded"]
    affected_runs = [r for r in runs if r.chaos_case_index in affected_cases]

    # --- detection and recovery latencies (per injection) ---
    detection: list[float] = []
    recovery: list[float] = []
    silent = 0
    step_by_key = {(st.run_id, st.node_key): st for st in steps}
    for inj in injections:
        if inj.run_id is None:
            continue
        reaction = next(
            (
                e
                for e in by_run_events.get(inj.run_id, [])
                if e.event_type in REACTION_EVENTS and e.timestamp >= inj.injected_at
            ),
            None,
        )
        if reaction is None:
            silent += 1
        else:
            detection.append((reaction.timestamp - inj.injected_at).total_seconds() * 1000)
        hit = step_by_key.get((inj.run_id, inj.step_key or ""))
        if hit and hit.status == "succeeded" and hit.completed_at and hit.completed_at >= inj.injected_at:
            recovery.append((hit.completed_at - inj.injected_at).total_seconds() * 1000)

    # --- duplicates ---
    dup = [i for i in injections if i.fault_type == "duplicate_delivery" and i.outcome]
    neutralized = 0
    for i in dup:
        o = i.outcome or {}
        if o.get("duplicate") == "step_completion":
            neutralized += 1 if o.get("result") == "rejected" else 0
        elif o.get("duplicate") == "tool_call":
            neutralized += 1 if (o.get("second") == "idempotent_replay" or not o.get("side_effecting")) else 0
    keys: dict[str, int] = {}
    for t in tools:
        if t.status == "succeeded" and t.tool_name in ("artifact_writer",):
            keys[t.idempotency_key] = keys.get(t.idempotency_key, 0) + 1
    duplicate_executions = sum(1 for v in keys.values() if v > 1)

    # --- consistency invariants ---
    checks = passed = 0
    violations: list[dict[str, Any]] = []
    attempts_by_step: dict[uuid.UUID, list[StepAttempt]] = {}
    for a in attempts:
        attempts_by_step.setdefault(a.step_id, []).append(a)

    def check(run: WorkflowRun, name: str, ok: bool) -> None:
        nonlocal checks, passed
        checks += 1
        passed += 1 if ok else 0
        if not ok:
            violations.append({"run_id": str(run.id), "case_index": run.chaos_case_index, "check": name})

    for r in runs:
        seqs = [e.sequence_number for e in by_run_events.get(r.id, [])]
        rs = by_run_steps.get(r.id, [])
        terminal = r.status in {s.value for s in TERMINAL_RUN}
        check(r, "event_sequence_contiguous", seqs == list(range(1, len(seqs) + 1)))
        check(
            r,
            "terminal_run_has_terminal_steps",
            (not terminal) or all(s.status in {x.value for x in TERMINAL_STEP} for s in rs),
        )
        check(
            r,
            "no_running_attempt_on_terminal_step",
            not any(
                a.status == "running"
                for s in rs
                if s.status in {x.value for x in TERMINAL_STEP}
                for a in attempts_by_step.get(s.id, [])
            ),
        )
        check(
            r,
            "succeeded_run_has_no_failed_step",
            r.status != "succeeded" or not any(s.status == "failed" for s in rs),
        )
        check(
            r,
            "no_lease_on_terminal_step",
            not any(s.lease_token is not None for s in rs if s.status in {x.value for x in TERMINAL_STEP}),
        )

    # --- budgets ---
    within = 0
    for r in runs:
        b = r.budget or {}
        ok = (
            float(r.cost_spent_usd) <= float(b.get("max_cost_usd", 1e9)) + 1e-9
            and r.model_calls <= int(b.get("max_model_calls", 10**9))
            and r.tool_calls <= int(b.get("max_tool_calls", 10**9))
        )
        within += 1 if ok else 0

    # --- policy under hostile output ---
    hostile_cases = {i.case_index for i in injections if i.fault_type == "malicious_tool_result"}
    hostile_runs = [r for r in runs if r.chaos_case_index in hostile_cases]
    bad_runs = {t.run_id for t in tools if t.tool_name == "artifact_writer" and t.status == "succeeded"} | {
        a.run_id for a in artifacts
    }
    held = sum(1 for r in hostile_runs if r.id not in bad_runs)

    n_steps = len(steps)
    n_attempts = len(attempts)
    by_type: dict[str, int] = {}
    for i in injections:
        by_type[i.fault_type] = by_type.get(i.fault_type, 0) + 1

    return {
        "planned_runs": planned,
        "runs_created": len(runs),
        "runs_finished": sum(1 for r in runs if r.status in {s.value for s in TERMINAL_RUN}),
        "injections_total": len(injections),
        "injections_by_type": by_type,
        "metrics": {
            "task_completion_rate": rate(len(succeeded), planned),
            "fault_exposure": rate(len(affected_runs), planned),
            "recovery_rate": rate(
                sum(1 for r in affected_runs if r.status == "succeeded"), len(affected_runs)
            ),
            "retry_amplification": {
                "attempts_per_step": (n_attempts / n_steps) if n_steps else None,
                "attempts": n_attempts,
                "steps": n_steps,
                "extra_attempts_per_injection": ((n_attempts - n_steps) / len(injections))
                if injections
                else None,
            },
            "time_to_detection_ms": {
                **summary(detection),
                "silent_faults": silent,
                "injections": len(injections),
            },
            "time_to_recovery_ms": summary(recovery),
            "duplicate_side_effect_prevention": {
                **rate(neutralized, len(dup)),
                "duplicate_successful_executions_found": duplicate_executions,
            },
            "state_consistency": {**rate(passed, checks), "violations": violations[:20]},
            "budget_compliance": rate(within, len(runs)),
            "policy_held_under_hostile_output": rate(held, len(hostile_runs)),
        },
        "definitions": METRIC_DEFINITIONS,
        "per_case": [
            {
                "case_index": r.chaos_case_index,
                "run_id": str(r.id),
                "status": r.status,
                "completed": r.status == "succeeded",
                "injections": sum(1 for i in injections if i.case_index == r.chaos_case_index),
                "attempts": sum(len(attempts_by_step.get(s.id, [])) for s in by_run_steps.get(r.id, [])),
                "error": (r.error or {}).get("first_error", {}).get("type")
                if isinstance(r.error, dict) and isinstance(r.error.get("first_error"), dict)
                else None,
            }
            for r in sorted(runs, key=lambda x: x.chaos_case_index or 0)
        ],
        "scoring_note": "No combined score is computed: the metrics measure different things on different scales.",
        "case_of_run": {str(k): v for k, v in case_of.items()},
    }
