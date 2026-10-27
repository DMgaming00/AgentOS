"""OpenEval integration: datasets, evaluation runs, scheduling, observation and grading.

Each (case, trial) executes as a real AgentOS workflow run (purpose='evaluation') with a
single agent_task node pinned to the exact agent version under test. The agent sees only
the task and input - never the expected output. Results are graded by OpenEval's
deterministic evaluators from what actually happened (output, tool executions, retrieved
sources, usage).
"""

from __future__ import annotations

import functools
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

import openeval
from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.events import emit
from agentos_core.autopilot.taxonomy import EVALUATOR_MAP, classify_error_type
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    ModelUsage,
    ToolExecution,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.domain.dag import WorkflowSpec
from agentos_core.domain.states import TERMINAL_RUN
from agentos_core.engine.core import AgentRef, materialize_run
from agentos_core.engine.tx import run_in_transaction
from agentos_core.log import get_logger
from agentos_core.util import canonical_json, sha256_hex, utcnow
from openeval.evaluators import derived_schema

log = get_logger(__name__)
HARNESS_WORKFLOW = "openeval-harness"


class EvaluationError(Exception):
    def __init__(self, message: str, code: str = "evaluation_error", status: int = 409) -> None:
        super().__init__(message)
        self.code, self.status = code, status


# ----------------------------------------------------------------------------- datasets


async def import_dataset(
    session: AsyncSession, workspace_id: uuid.UUID, ds: openeval.Dataset, *, created_by: uuid.UUID | None
) -> tuple[EvaluationDataset, bool]:
    """Store a dataset version. Re-importing identical content is a no-op; changed content needs a new version."""
    digest = ds.content_hash()
    existing = (
        await session.execute(
            select(EvaluationDataset).where(
                EvaluationDataset.workspace_id == workspace_id,
                EvaluationDataset.name == ds.name,
                EvaluationDataset.split == ds.split,
                EvaluationDataset.version == ds.version,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.dataset_hash != digest:
            raise EvaluationError(
                f"{ds.name} {ds.split} v{ds.version} already exists with different content; bump the version",
                code="dataset_version_conflict",
            )
        return existing, False
    row = EvaluationDataset(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        name=ds.name,
        split=ds.split,
        version=ds.version,
        visibility=ds.visibility,
        description=ds.description,
        dataset_hash=digest,
        case_count=len(ds.cases),
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    for c in ds.cases:
        session.add(
            EvaluationCase(
                id=uuid.uuid4(),
                dataset_id=row.id,
                case_key=c.case_id,
                task=c.task,
                input={**c.input, "input_artifacts": c.input_artifacts},
                expected_output=c.expected_output,
                evaluator_config={"methods": c.evaluation_methods, "config": c.evaluator_config},
                tags=c.tags,
            )
        )
    await session.flush()
    await emit(
        session,
        workspace_id,
        "dataset.imported",
        {
            "dataset_id": str(row.id),
            "name": ds.name,
            "split": ds.split,
            "version": ds.version,
            "cases": len(ds.cases),
            "hash": digest,
        },
    )
    return row, True


def to_case(row: EvaluationCase) -> openeval.Case:
    inp = dict(row.input)
    artifacts = inp.pop("input_artifacts", [])
    return openeval.Case(
        case_id=row.case_key,
        task=row.task,
        input=inp,
        input_artifacts=artifacts,
        expected_output=row.expected_output,
        evaluation_methods=row.evaluator_config["methods"],
        evaluator_config=row.evaluator_config.get("config", {}),
        tags=list(row.tags),
    )


async def dataset_cases(session: AsyncSession, dataset_id: uuid.UUID) -> list[EvaluationCase]:
    return list(
        (
            await session.execute(
                select(EvaluationCase)
                .where(EvaluationCase.dataset_id == dataset_id)
                .order_by(EvaluationCase.case_key)
            )
        ).scalars()
    )


# ----------------------------------------------------------------------------- runs


async def harness_workflow(session: AsyncSession, workspace_id: uuid.UUID) -> WorkflowDefinition:
    wf = (
        await session.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.workspace_id == workspace_id, WorkflowDefinition.name == HARNESS_WORKFLOW
            )
        )
    ).scalar_one_or_none()
    if wf is None:
        # Concurrent coordinators may race to create it: insert-if-absent, then read.
        await session.execute(
            pg_insert(WorkflowDefinition)
            .values(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                name=HARNESS_WORKFLOW,
                version=1,
                description="Internal: one run per OpenEval case execution",
                definition={"mode": "evaluation"},
            )
            .on_conflict_do_nothing(constraint="uq_workflow_definitions_name_version")
        )
        wf = (
            await session.execute(
                select(WorkflowDefinition).where(
                    WorkflowDefinition.workspace_id == workspace_id,
                    WorkflowDefinition.name == HARNESS_WORKFLOW,
                )
            )
        ).scalar_one()
    return wf


def conditions_for(
    agent: AgentDefinition, version: AgentConfigVersion, dataset: EvaluationDataset, trials: int
) -> dict[str, Any]:
    """Everything needed to reproduce the run (recorded, never inferred later)."""
    cfg = AgentConfig.model_validate(version.config_snapshot)
    from agentos_core.tools.registry import get_registry

    tools = [
        {
            "name": n,
            "description": cfg.tool_descriptions.get(n, t.description),
            "schema_hash": sha256_hex(canonical_json(t.input_schema()))[:16],
        }
        for n in cfg.allowed_tool_ids
        if (t := get_registry().get(n)) is not None
    ]
    return {
        "agent_name": agent.name,
        "agent_version": version.version,
        "agent_version_id": str(version.id),
        "config_hash": version.config_hash,
        "provider": cfg.provider,
        "model": cfg.model,
        "simulated": cfg.provider == "mock",
        "effort": cfg.model_configuration.get("effort"),
        "temperature": cfg.model_configuration.get("temperature"),
        "max_tokens": cfg.model_configuration.get("max_tokens"),
        "prompt_hash": sha256_hex(cfg.system_instructions + "\n" + cfg.tool_strategy)[:16],
        "tools": tools,
        "max_steps": cfg.max_steps,
        "trials": trials,
        "seed": "deterministic (mock)" if cfg.provider == "mock" else "not supported by provider",
        "dataset": {
            "id": str(dataset.id),
            "name": dataset.name,
            "split": dataset.split,
            "version": dataset.version,
            "hash": dataset.dataset_hash,
            "cases": dataset.case_count,
        },
        "created_at": utcnow().isoformat(),
    }


async def create_evaluation_run(
    session: AsyncSession,
    *,
    agent: AgentDefinition,
    version: AgentConfigVersion,
    dataset: EvaluationDataset,
    trials: int,
    created_by: uuid.UUID | None,
    max_concurrency: int,
    per_case_max_cost_usd: float,
    experiment_id: uuid.UUID | None = None,
    arm: str | None = None,
    interleave_offset: int = 0,
) -> EvaluationRun:
    if version.agent_id != agent.id:
        raise EvaluationError("version does not belong to this agent", code="invalid_version", status=422)
    if dataset.workspace_id != agent.workspace_id:
        raise EvaluationError("dataset not found", code="not_found", status=404)
    cases = await dataset_cases(session, dataset.id)
    await harness_workflow(session, agent.workspace_id)
    run = EvaluationRun(
        id=uuid.uuid4(),
        workspace_id=agent.workspace_id,
        agent_id=agent.id,
        agent_version_id=version.id,
        dataset_id=dataset.id,
        experiment_id=experiment_id,
        arm=arm,
        trials=trials,
        max_concurrency=max_concurrency,
        max_cost_usd=Decimal(str(per_case_max_cost_usd)) * len(cases) * trials,
        status="queued",
        conditions={
            **conditions_for(agent, version, dataset, trials),
            "per_case_max_cost_usd": per_case_max_cost_usd,
        },
        created_by=created_by,
    )
    session.add(run)
    await session.flush()
    for i, case in enumerate(cases):
        for attempt in range(1, trials + 1):
            session.add(
                EvaluationCaseResult(
                    id=uuid.uuid4(),
                    evaluation_run_id=run.id,
                    case_id=case.id,
                    attempt=attempt,
                    # Experiments interleave baseline/candidate executions to reduce drift between arms.
                    sort_key=((i * trials) + attempt - 1) * 2 + interleave_offset,
                    status="queued",
                )
            )
    await session.flush()
    await emit(
        session,
        agent.workspace_id,
        "evaluation.started",
        {
            "evaluation_run_id": str(run.id),
            "agent": agent.name,
            "version": version.version,
            "dataset": f"{dataset.name}/{dataset.split} v{dataset.version}",
            "executions": len(cases) * trials,
            "experiment_id": str(experiment_id) if experiment_id else None,
            "arm": arm,
        },
    )
    return run


def estimated_max_cost(case_count: int, trials: int, per_case_max_cost_usd: float) -> float:
    return case_count * trials * per_case_max_cost_usd


# ----------------------------------------------------------------------------- scheduling & grading


async def evaluation_tick() -> dict[str, int]:
    """Advance all active evaluation runs. Safe to run concurrently from several workers."""
    from agentos_core.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        ids = (
            (
                await s.execute(
                    select(EvaluationRun.id)
                    .where(EvaluationRun.status.in_(["queued", "running"]))
                    .order_by(EvaluationRun.created_at)
                )
            )
            .scalars()
            .all()
        )
    stats = {"scheduled": 0, "graded": 0, "completed": 0}
    for run_id in ids:
        res = await run_in_transaction(functools.partial(_advance, run_id=run_id))
        for k, v in res.items():
            stats[k] += v
    return stats


async def _advance(session: AsyncSession, *, run_id: uuid.UUID) -> dict[str, int]:
    out = {"scheduled": 0, "graded": 0, "completed": 0}
    ev = (
        await session.execute(
            select(EvaluationRun)
            .where(EvaluationRun.id == run_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if ev is None or ev.status not in ("queued", "running"):
        return out
    results = list(
        (
            await session.execute(
                select(EvaluationCaseResult)
                .where(EvaluationCaseResult.evaluation_run_id == run_id)
                .order_by(EvaluationCaseResult.sort_key)
                .with_for_update()
            )
        ).scalars()
    )

    # 1. grade finished executions
    running = [r for r in results if r.status == "running"]
    if running:
        terminal = dict(
            (
                await session.execute(
                    select(WorkflowRun.id, WorkflowRun.status).where(
                        WorkflowRun.id.in_([r.workflow_run_id for r in running]),
                        WorkflowRun.status.in_(list(TERMINAL_RUN)),
                    )
                )
            ).all()
        )
        cases = {
            c.id: c
            for c in (
                await session.execute(
                    select(EvaluationCase).where(EvaluationCase.id.in_([r.case_id for r in running]))
                )
            ).scalars()
        }
        for r in running:
            if r.workflow_run_id in terminal:
                await _grade(session, r, cases[r.case_id])
                out["graded"] += 1

    # 2. budget cap: once spend reaches the cap, remaining executions are recorded as not run
    spent = sum(float((r.usage or {}).get("cost_usd") or 0) for r in results if r.status == "graded")
    queued = [r for r in results if r.status == "queued"]
    if queued and spent >= float(ev.max_cost_usd):
        for r in queued:
            r.status, r.passed, r.graded_at = "graded", None, utcnow()
            r.error = {"type": "not_run", "reason": "evaluation budget exhausted", "spent_usd": spent}
        queued = []

    # 3. schedule within the concurrency limit
    in_flight = sum(1 for r in results if r.status == "running")
    if queued and in_flight < ev.max_concurrency:
        agent = await session.get(AgentDefinition, ev.agent_id)
        assert agent is not None
        wf = await harness_workflow(session, ev.workspace_id)
        cases = {
            c.id: c
            for c in (
                await session.execute(
                    select(EvaluationCase).where(EvaluationCase.id.in_([r.case_id for r in queued]))
                )
            ).scalars()
        }
        version = await session.get(AgentConfigVersion, ev.agent_version_id)
        assert version is not None
        cfg = AgentConfig.model_validate(version.config_snapshot)
        for r in queued[: ev.max_concurrency - in_flight]:
            r.workflow_run_id = (
                await _start_case(session, ev, agent, wf, cfg, cases[r.case_id], r, len(results))
            ).id
            r.status = "running"
            out["scheduled"] += 1
        if ev.status == "queued":
            ev.status, ev.started_at = "running", utcnow()

    # 4. finalize
    if all(r.status == "graded" for r in results):
        await _finalize(session, ev, results)
        out["completed"] += 1
    return out


async def _start_case(
    session: AsyncSession,
    ev: EvaluationRun,
    agent: AgentDefinition,
    wf: WorkflowDefinition,
    cfg: AgentConfig,
    case: EvaluationCase,
    result: EvaluationCaseResult,
    planned: int,
) -> WorkflowRun:
    methods = case.evaluator_config["methods"]
    node: dict[str, Any] = {
        "key": "case",
        "type": "agent_task",
        "agent": agent.name,
        "name": f"OpenEval {case.case_key}",
        "prompt": case.task,
        # Only the task and its inputs reach the agent; expected_output never does.
        "input": {k: v for k, v in case.input.items() if k != "input_artifacts"},
        "timeout_seconds": min(cfg.timeout_seconds, 600),
        "retry": {"max_attempts": 2, "backoff_base_seconds": 0.5, "backoff_max_seconds": 2},
    }
    if "schema_validation" in methods:
        node["output_contract"] = case.evaluator_config.get("config", {}).get("schema_validation", {}).get(
            "schema"
        ) or derived_schema(case.expected_output)
    # Two independent limits: each execution's own run budget, and the evaluation's total cap (in _advance).
    per_case = float(ev.conditions.get("per_case_max_cost_usd") or (float(ev.max_cost_usd) / max(1, planned)))
    spec = WorkflowSpec.model_validate(
        {
            "nodes": [node],
            "budget": {
                "max_model_calls": max(4, cfg.max_steps * 2),
                "max_tool_calls": 50,
                "max_cost_usd": min(per_case, float(Decimal(cfg.max_cost_usd))),
                "max_concurrency": 1,
                "max_duration_seconds": 900,
            },
        }
    )
    return await materialize_run(
        session,
        workflow=wf,
        spec=spec,
        run_input={"evaluation_run_id": str(ev.id), "case_key": case.case_key, "attempt": result.attempt},
        requested_by=ev.created_by,
        agents_by_name={agent.name: AgentRef(agent.id, agent.name)},
        version_overrides={agent.id: ev.agent_version_id},
        purpose="evaluation",
    )


async def observe_run(
    session: AsyncSession, run_id: uuid.UUID
) -> tuple[openeval.Observation, dict[str, Any]]:
    run = (await session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))).scalar_one()
    step = (
        (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_id))).scalars().first()
    )
    tes = (
        (
            await session.execute(
                select(ToolExecution).where(ToolExecution.run_id == run_id).order_by(ToolExecution.created_at)
            )
        )
        .scalars()
        .all()
    )
    usage = (
        await session.execute(
            select(func.count(), func.sum(ModelUsage.input_tokens), func.sum(ModelUsage.output_tokens)).where(
                ModelUsage.run_id == run_id
            )
        )
    ).one()
    sources: dict[str, str] = {}
    for te in tes:
        if te.tool_name == "document_search" and te.status == "succeeded":
            for hit in (te.result or {}).get("results", []):
                sources[hit["citation"]] = hit.get("content", "")
    ok = step is not None and step.status == "succeeded"
    error = None if ok else (step.error if step and step.error else {"type": run.status})
    latency = (
        ((run.completed_at - run.started_at).total_seconds() * 1000)
        if run.completed_at and run.started_at
        else None
    )
    obs = openeval.Observation(
        output=step.output if ok and step else None,
        tool_calls=[
            openeval.ToolCallRecord(
                name=te.tool_name,
                arguments=te.arguments,
                status=te.status,
                error_type=(te.error or {}).get("type"),
            )
            for te in tes
        ],
        sources=sources,
        error=error,
        latency_ms=latency,
        cost_usd=float(run.cost_spent_usd),
        retries=step.failure_count if step else 0,
    )
    return obs, {
        "cost_usd": float(run.cost_spent_usd),
        "latency_ms": latency,
        "model_calls": usage[0],
        "input_tokens": usage[1],
        "output_tokens": usage[2],
        "tool_calls": len(tes),
        # identical calls asked again by the same attempt and answered from the recorded result (not re-executed)
        "tool_replays": sum(te.replay_count for te in tes),
        "retries": obs.retries,
    }


async def _grade(session: AsyncSession, result: EvaluationCaseResult, case: EvaluationCase) -> None:
    assert result.workflow_run_id is not None
    obs, usage = await observe_run(session, result.workflow_run_id)
    grade = openeval.grade_case(to_case(case), obs)
    category = None
    if obs.error is not None:
        category = classify_error_type(obs.error.get("type"), str(obs.error.get("message", ""))).value
    elif not grade.passed:
        first = next(r for r in grade.results if not r.passed and not r.advisory)
        mapped = EVALUATOR_MAP.get(first.method)
        category = mapped.value if mapped else None
    result.status, result.passed, result.graded_at = "graded", grade.passed, utcnow()
    result.observed_output = obs.output
    result.scores = {r.method: r.to_dict() for r in grade.results}
    result.usage = usage
    result.error = (
        {**obs.error, "category": category}
        if obs.error
        else ({"type": "evaluation_failed", "category": category} if category else None)
    )


def records_from(
    results: list[EvaluationCaseResult], case_keys: dict[uuid.UUID, str]
) -> list[openeval.CaseRecord]:
    """Executed results only; not-run executions are excluded and reported as incomplete."""
    return [
        openeval.CaseRecord(
            case_id=case_keys[r.case_id],
            attempt=r.attempt,
            passed=bool(r.passed),
            method_passed={
                m: bool(v.get("passed")) for m, v in (r.scores or {}).items() if not v.get("advisory")
            },
            errored=r.observed_output is None,
            error_category=(r.error or {}).get("category"),
            cost_usd=(r.usage or {}).get("cost_usd"),
            latency_ms=(r.usage or {}).get("latency_ms"),
            retries=int((r.usage or {}).get("retries") or 0),
        )
        for r in results
        if r.passed is not None
    ]


async def case_keys_for(session: AsyncSession, dataset_id: uuid.UUID) -> dict[uuid.UUID, str]:
    return dict(
        (
            await session.execute(
                select(EvaluationCase.id, EvaluationCase.case_key).where(
                    EvaluationCase.dataset_id == dataset_id
                )
            )
        ).all()
    )


async def _finalize(session: AsyncSession, ev: EvaluationRun, results: list[EvaluationCaseResult]) -> None:
    keys = await case_keys_for(session, ev.dataset_id)
    metrics = openeval.aggregate(records_from(results, keys))
    incomplete = sum(1 for r in results if r.passed is None)
    metrics["incomplete_executions"] = incomplete
    metrics["planned_executions"] = len(results)
    ev.metrics = metrics
    ev.completed_at = utcnow()
    ev.conditions = {
        **ev.conditions,
        "completed_at": ev.completed_at.isoformat(),
        "started_at": ev.started_at.isoformat() if ev.started_at else None,
    }
    ev.status = "completed"
    await session.flush()
    rate = metrics["task_success_rate"]
    await emit(
        session,
        ev.workspace_id,
        "evaluation.completed",
        {
            "evaluation_run_id": str(ev.id),
            "success": rate,
            "incomplete": incomplete,
            "experiment_id": str(ev.experiment_id) if ev.experiment_id else None,
            "arm": ev.arm,
        },
    )


async def evaluation_report(session: AsyncSession, ev: EvaluationRun) -> dict[str, Any]:
    dataset = await session.get(EvaluationDataset, ev.dataset_id)
    assert dataset is not None
    rows = (
        await session.execute(
            select(EvaluationCaseResult, EvaluationCase.case_key)
            .join(EvaluationCase, EvaluationCase.id == EvaluationCaseResult.case_id)
            .where(EvaluationCaseResult.evaluation_run_id == ev.id)
            .order_by(EvaluationCase.case_key, EvaluationCaseResult.attempt)
        )
    ).all()
    return {
        "evaluation_run_id": str(ev.id),
        "status": ev.status,
        "dataset": {
            "id": str(dataset.id),
            "name": dataset.name,
            "split": dataset.split,
            "version": dataset.version,
            "hash": dataset.dataset_hash,
        },
        "conditions": ev.conditions,
        "metrics": ev.metrics,
        "results": [
            {
                "case_key": key,
                "attempt": r.attempt,
                "passed": r.passed,
                "scores": r.scores,
                "usage": r.usage,
                "error": r.error,
                "workflow_run_id": str(r.workflow_run_id) if r.workflow_run_id else None,
            }
            for r, key in rows
        ],
    }
