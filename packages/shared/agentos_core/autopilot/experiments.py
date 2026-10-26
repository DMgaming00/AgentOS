"""Controlled baseline-vs-candidate experiments on matched OpenEval cases."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import openeval
from agentos_core.autopilot.config import AgentConfig, config_diff
from agentos_core.autopilot.evaluation import (
    EvaluationError,
    case_keys_for,
    create_evaluation_run,
    dataset_cases,
    estimated_max_cost,
    records_from,
)
from agentos_core.autopilot.events import emit
from agentos_core.autopilot.gates import evaluate_gates
from agentos_core.autopilot.policy import load_policy
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    OptimizationCandidate,
    OptimizationExperiment,
    ToolExecution,
    Workspace,
)
from agentos_core.engine.tx import run_in_transaction
from agentos_core.util import utcnow

CANDIDATE_STATUS = {"eligible": "eligible", "not_eligible": "not_eligible", "inconclusive": "inconclusive"}


async def start_experiment(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    dataset_id: uuid.UUID,
    trials: int,
    user_id: uuid.UUID | None,
    candidate_id: uuid.UUID | None = None,
    baseline_version_id: uuid.UUID | None = None,
    candidate_version_id: uuid.UUID | None = None,
    confirm_cost: bool = False,
) -> OptimizationExperiment:
    ws = await session.get(Workspace, workspace_id)
    agent = await session.get(AgentDefinition, agent_id)
    if ws is None or agent is None or agent.workspace_id != workspace_id:
        raise EvaluationError("agent not found", code="not_found", status=404)
    policy = load_policy(ws.settings)
    cand = None
    if candidate_id:
        cand = await session.get(OptimizationCandidate, candidate_id)
        if cand is None or cand.agent_id != agent_id:
            raise EvaluationError("candidate not found", code="not_found", status=404)
        if cand.candidate_version_id is None:
            raise EvaluationError(
                "candidate was rejected by policy and cannot be evaluated",
                code="candidate_rejected",
                status=422,
            )
        baseline_version_id, candidate_version_id = cand.baseline_version_id, cand.candidate_version_id
    if not baseline_version_id or not candidate_version_id or baseline_version_id == candidate_version_id:
        raise EvaluationError("need two different versions to compare", code="invalid_experiment", status=422)
    base_v = await session.get(AgentConfigVersion, baseline_version_id)
    cand_v = await session.get(AgentConfigVersion, candidate_version_id)
    if base_v is None or cand_v is None or base_v.agent_id != agent_id or cand_v.agent_id != agent_id:
        raise EvaluationError("versions must belong to this agent", code="invalid_version", status=422)
    dataset = await session.get(EvaluationDataset, dataset_id)
    if dataset is None or dataset.workspace_id != workspace_id:
        raise EvaluationError("dataset not found", code="not_found", status=404)
    if dataset.split == "development":
        # Development cases are visible to the optimizer; scoring candidates on them would reward overfitting.
        raise EvaluationError(
            "experiments must use a validation or held-out test split", code="leaky_dataset", status=422
        )
    if trials > policy.max_eval_trials:
        raise EvaluationError(
            f"trials must be <= {policy.max_eval_trials}", code="too_many_trials", status=422
        )
    n_cases = len(await dataset_cases(session, dataset.id))
    est = 2 * estimated_max_cost(n_cases, trials, policy.per_case_max_cost_usd)
    paid = any(AgentConfig.model_validate(v.config_snapshot).provider != "mock" for v in (base_v, cand_v))
    if paid and est > policy.paid_confirmation_threshold_usd and not confirm_cost:
        raise EvaluationError(
            f"this experiment may cost up to ${est:.2f} with a paid provider; resend with confirm_cost=true",
            code="cost_confirmation_required",
            status=409,
        )
    exp = OptimizationExperiment(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        agent_id=agent_id,
        candidate_id=candidate_id,
        baseline_version_id=base_v.id,
        candidate_version_id=cand_v.id,
        validation_dataset_id=dataset.id,
        trials=trials,
        status="running",
        created_by=user_id,
        conditions={
            "dataset": {
                "name": dataset.name,
                "split": dataset.split,
                "version": dataset.version,
                "hash": dataset.dataset_hash,
                "cases": n_cases,
            },
            "trials": trials,
            "estimated_max_cost_usd": est,
            "paid_provider": paid,
            "interleaved": True,
            "gates": policy.gates.model_dump(),
        },
    )
    session.add(exp)
    await session.flush()
    for arm, version, offset in (("baseline", base_v, 0), ("candidate", cand_v, 1)):
        await create_evaluation_run(
            session,
            agent=agent,
            version=version,
            dataset=dataset,
            trials=trials,
            created_by=user_id,
            max_concurrency=policy.max_eval_concurrency,
            per_case_max_cost_usd=policy.per_case_max_cost_usd,
            experiment_id=exp.id,
            arm=arm,
            interleave_offset=offset,
        )
    if cand is not None:
        cand.status = "evaluating"
    await emit(
        session,
        workspace_id,
        "experiment.started",
        {
            "experiment_id": str(exp.id),
            "agent": agent.name,
            "baseline": base_v.version,
            "candidate": cand_v.version,
            "dataset": f"{dataset.name}/{dataset.split}",
            "trials": trials,
        },
    )
    return exp


async def experiments_tick() -> int:
    from agentos_core.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        ids = (
            (
                await s.execute(
                    select(OptimizationExperiment.id).where(OptimizationExperiment.status == "running")
                )
            )
            .scalars()
            .all()
        )
    done = 0
    for exp_id in ids:

        async def _do(session: AsyncSession, exp_id: uuid.UUID = exp_id) -> int:
            return await _maybe_finalize(session, exp_id)

        done += await run_in_transaction(_do)
    return done


async def _maybe_finalize(session: AsyncSession, exp_id: uuid.UUID) -> int:
    exp = (
        await session.execute(
            select(OptimizationExperiment)
            .where(OptimizationExperiment.id == exp_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if exp is None or exp.status != "running":
        return 0
    runs = {
        r.arm: r
        for r in (
            await session.execute(select(EvaluationRun).where(EvaluationRun.experiment_id == exp_id))
        ).scalars()
    }
    if any(r.status in ("failed", "cancelled") for r in runs.values()):
        exp.status, exp.completed_at = "failed", utcnow()
        exp.results = {"error": "an evaluation arm failed or was cancelled"}
        return 1
    if set(runs) != {"baseline", "candidate"} or any(r.status != "completed" for r in runs.values()):
        return 0
    results, eligibility = await compute_results(session, exp, runs["baseline"], runs["candidate"])
    exp.results, exp.eligibility, exp.decision = results, eligibility, eligibility["decision"]
    exp.completed_at = utcnow()
    exp.status = "completed"
    if exp.candidate_id:
        cand = await session.get(OptimizationCandidate, exp.candidate_id)
        if cand is not None and cand.status in ("evaluating", "proposed"):
            cand.status = CANDIDATE_STATUS[eligibility["decision"]]
    await emit(
        session,
        exp.workspace_id,
        "experiment.completed",
        {
            "experiment_id": str(exp.id),
            "decision": exp.decision,
            "improvement_demonstrated": eligibility["improvement_demonstrated"],
            "summary": eligibility["summary"],
        },
    )
    return 1


async def compute_results(
    session: AsyncSession, exp: OptimizationExperiment, base_run: EvaluationRun, cand_run: EvaluationRun
) -> tuple[dict[str, Any], dict[str, Any]]:
    ws = await session.get(Workspace, exp.workspace_id)
    policy = load_policy(ws.settings if ws else {})
    keys = await case_keys_for(session, exp.validation_dataset_id)
    tags: dict[str, list[str]] = dict(
        (
            await session.execute(
                select(EvaluationCase.case_key, EvaluationCase.tags).where(
                    EvaluationCase.dataset_id == exp.validation_dataset_id
                )
            )
        ).all()
    )
    by_arm: dict[str, list[EvaluationCaseResult]] = {}
    for arm, run in (("baseline", base_run), ("candidate", cand_run)):
        by_arm[arm] = list(
            (
                await session.execute(
                    select(EvaluationCaseResult).where(EvaluationCaseResult.evaluation_run_id == run.id)
                )
            ).scalars()
        )
    recs = {arm: records_from(rows, keys) for arm, rows in by_arm.items()}
    comparison = openeval.compare(
        recs["baseline"], recs["candidate"], min_n=policy.gates.min_validation_cases
    )
    denied = (
        await session.execute(
            select(func.count())
            .select_from(ToolExecution)
            .join(EvaluationCaseResult, EvaluationCaseResult.workflow_run_id == ToolExecution.run_id)
            .where(EvaluationCaseResult.evaluation_run_id == cand_run.id, ToolExecution.status == "denied")
        )
    ).scalar_one()
    critical = [
        k for k in comparison.regressions if set(tags.get(k, [])) & set(policy.gates.no_regression_tags)
    ]
    eligibility = evaluate_gates(
        comparison=comparison.to_dict(),
        baseline=base_run.metrics or {},
        candidate=cand_run.metrics or {},
        gates=policy.gates,
        denied_tool_calls=denied,
        critical_regressions=critical,
    )

    def failing(rows: list[EvaluationCaseResult]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in rows:
            fails = [m for m, v in (r.scores or {}).items() if not v.get("passed") and not v.get("advisory")]
            if r.passed is None:
                fails = ["not_run"]
            elif r.observed_output is None:
                fails = [f"error:{(r.error or {}).get('type')}", *fails]
            out.setdefault(keys[r.case_id], []).extend(fails)
        return out

    def lat(rows: list[EvaluationCaseResult]) -> list[float]:
        return [float(v) for r in rows if (v := (r.usage or {}).get("latency_ms")) is not None]

    fb, fc = failing(by_arm["baseline"]), failing(by_arm["candidate"])
    per_case = [
        {
            **row,
            "baseline_failing": sorted(set(fb.get(row["case_id"], []))),
            "candidate_failing": sorted(set(fc.get(row["case_id"], []))),
            "tags": tags.get(row["case_id"], []),
        }
        for row in comparison.per_case
    ]
    base_v = await session.get(AgentConfigVersion, exp.baseline_version_id)
    cand_v = await session.get(AgentConfigVersion, exp.candidate_version_id)
    bm, cm = base_run.metrics or {}, cand_run.metrics or {}

    def delta(a: Any, b: Any) -> float | None:
        return None if a in (None, 0) or b is None else (b - a) / a

    results = {
        "baseline": {
            "version": base_v.version if base_v else None,
            "metrics": bm,
            "conditions": base_run.conditions,
            "evaluation_run_id": str(base_run.id),
            "latencies_ms": lat(by_arm["baseline"]),
        },
        "candidate": {
            "version": cand_v.version if cand_v else None,
            "metrics": cm,
            "conditions": cand_run.conditions,
            "evaluation_run_id": str(cand_run.id),
            "latencies_ms": lat(by_arm["candidate"]),
        },
        "comparison": comparison.to_dict() | {"per_case": per_case},
        "cost": {
            "baseline_per_success": bm.get("cost_per_successful_task_usd"),
            "candidate_per_success": cm.get("cost_per_successful_task_usd"),
            "baseline_total": bm.get("total_cost_usd"),
            "candidate_total": cm.get("total_cost_usd"),
            "per_success_change": delta(
                bm.get("cost_per_successful_task_usd"), cm.get("cost_per_successful_task_usd")
            ),
            "total_change": delta(bm.get("total_cost_usd"), cm.get("total_cost_usd")),
        },
        "latency": {"baseline": bm.get("latency_ms"), "candidate": cm.get("latency_ms")},
        "error_distribution": {
            "baseline": bm.get("failure_breakdown"),
            "candidate": cm.get("failure_breakdown"),
        },
        "denied_tool_calls": denied,
        "diff": config_diff(base_v.config_snapshot, cand_v.config_snapshot) if base_v and cand_v else [],
        "notes": [
            "Same dataset version, same cases, interleaved execution order.",
            "Mock provider is deterministic: repeated trials produce identical outcomes."
            if cm and (cand_run.conditions or {}).get("simulated")
            else "Real models are nondeterministic: use trials > 1 for important comparisons.",
        ],
    }
    return results, eligibility
