"""Promotion gates: facts checked in code before a candidate may reach staging or production.

``passed`` is ``True``, ``False`` or ``None`` (pending: a human decision that has not happened yet). Nothing here is a
model judgment; every gate reads recorded results (the paired experiment, the latest Security Center evaluation, budgets).

Note on security: the Security Center evaluates the *platform's enforcement layer* for the whole workspace, not one agent's
version, so this gate says "the enforcement layer passed its tests recently", not "this candidate is safe".
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.policy import load_policy
from agentos_core.db.models import (
    AgentDefinition,
    FinopsBudget,
    OptimizationExperiment,
    SecurityEvaluationRun,
    Workspace,
)
from agentos_core.finops.budgets import utilization
from agentos_core.util import utcnow

SECURITY_MAX_AGE = timedelta(days=7)


def gate(name: str, passed: bool | None, detail: str, **measured: Any) -> dict[str, Any]:
    return {"gate": name, "passed": passed, "detail": detail, "measured": measured}


async def security_gate(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Any]:
    run = (
        await session.execute(
            select(SecurityEvaluationRun)
            .where(
                SecurityEvaluationRun.workspace_id == workspace_id,
                SecurityEvaluationRun.status == "completed",
            )
            .order_by(SecurityEvaluationRun.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if run is None or run.results is None:
        return gate(
            "security_evaluation", False, "no completed Security Center evaluation exists; run one first"
        )
    age = utcnow() - (run.completed_at or run.created_at)
    m = (run.results or {}).get("metrics", {})
    counts = (run.results or {}).get("counts", {})
    unsafe = m.get("unsafe_side_effects")
    enforcement = (m.get("successful_policy_enforcement") or {}).get("value")
    failing = sum(c.get("fail", 0) + c.get("error", 0) for c in counts.values())
    ok = unsafe == 0 and enforcement == 1.0 and failing == 0 and age <= SECURITY_MAX_AGE
    detail = (
        f"latest evaluation {run.completed_at:%Y-%m-%d %H:%M} UTC: unsafe side effects {unsafe}, enforcement {enforcement}, "
        f"{failing} failing/error cases"
    )
    if age > SECURITY_MAX_AGE:
        detail += f"; older than {SECURITY_MAX_AGE.days} days"
    return gate(
        "security_evaluation",
        ok,
        detail,
        run_id=str(run.id),
        unsafe_side_effects=unsafe,
        failing_cases=failing,
        age_hours=round(age.total_seconds() / 3600, 1),
    )


async def budget_gate(
    session: AsyncSession, workspace_id: uuid.UUID, agent_id: uuid.UUID, exp: OptimizationExperiment | None
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(FinopsBudget).where(
                FinopsBudget.workspace_id == workspace_id,
                FinopsBudget.enabled.is_(True),
                (FinopsBudget.scope == "workspace") | (FinopsBudget.agent_id == agent_id),
            )
        )
    ).scalars()
    exceeded = []
    for b in rows:
        u = await utilization(session, b)
        if u["state"] == "exceeded":
            exceeded.append({"scope": b.scope, "period": b.period, "utilization": round(u["utilization"], 3)})
    cost_gate = (
        next(
            (
                g
                for g in ((exp.eligibility or {}).get("gates") or [])
                if g["gate"] == "cost_per_successful_task"
            ),
            None,
        )
        if exp
        else None
    )
    cost_ok = cost_gate is None or cost_gate.get("passed", False)
    ok = not exceeded and cost_ok
    detail = (
        "no budget is exceeded and the experiment's cost gate passed"
        if ok
        else (
            f"exceeded budgets: {exceeded}"
            if exceeded
            else "the experiment's cost-per-successful-task gate did not pass"
        )
    )
    return gate("budget_compliance", ok, detail, exceeded_budgets=exceeded)


async def promotion_gates(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent: AgentDefinition,
    experiment: OptimizationExperiment | None,
    require_approval: bool,
) -> list[dict[str, Any]]:
    ws = await session.get(Workspace, workspace_id)
    policy = load_policy(ws.settings if ws else {})
    out: list[dict[str, Any]] = []
    if experiment is None:
        out.append(gate("experiment_eligible", False, "an eligible paired experiment is required"))
    else:
        stale = agent.active_version_id != experiment.baseline_version_id
        eligible = experiment.status == "completed" and experiment.decision == "eligible"
        out.append(
            gate(
                "experiment_eligible",
                eligible and not stale,
                "experiment decision: "
                + (experiment.decision or experiment.status)
                + ("; STALE: the active version changed after it ran" if stale else ""),
                experiment_id=str(experiment.id),
            )
        )
        res = experiment.results or {}
        comp = res.get("comparison") or {}
        cand = (res.get("candidate") or {}).get("metrics") or {}
        success = (cand.get("task_success_rate") or {}).get("value")
        out.append(
            gate(
                "task_quality",
                success is not None and success >= policy.gates.min_candidate_success_rate,
                f"candidate success rate {success} (minimum {policy.gates.min_candidate_success_rate})",
                success_rate=success,
            )
        )
        regressions = comp.get("regressions") or []
        crit = next(
            (
                g
                for g in (experiment.eligibility or {}).get("gates", [])
                if g["gate"] == "critical_case_regressions"
            ),
            None,
        )
        out.append(
            gate(
                "regression_level",
                len(regressions) <= policy.gates.max_regressions
                and (crit is None or crit.get("passed", False)),
                f"{len(regressions)} per-case regressions (allowed {policy.gates.max_regressions}); critical-tag regressions {'none' if crit is None or crit.get('passed') else 'present'}",
                regressions=regressions,
            )
        )
        n = comp.get("n_matched", 0)
        out.append(
            gate(
                "minimum_sample_size",
                n >= policy.gates.min_validation_cases,
                f"{n} matched cases (minimum {policy.gates.min_validation_cases})",
                n=n,
            )
        )
    out.append(await budget_gate(session, workspace_id, agent.id, experiment))
    out.append(await security_gate(session, workspace_id))
    if require_approval:
        out.append(gate("human_approval", None, "an administrator must approve the production canary"))
    return out


def blocking(gates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [g for g in gates if g["passed"] is False]
