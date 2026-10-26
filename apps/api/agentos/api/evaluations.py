"""OpenEval API: datasets, evaluation runs, results, exports."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select

import openeval
from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import Page
from agentos.schemas.autopilot import (
    CaseOut,
    DatasetDetail,
    DatasetOut,
    EvaluationRunOut,
    EvaluationRunRequest,
)
from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.evaluation import (
    EvaluationError,
    create_evaluation_run,
    dataset_cases,
    estimated_max_cost,
    evaluation_report,
    import_dataset,
)
from agentos_core.autopilot.policy import load_policy
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    Workspace,
)
from agentos_core.domain.states import Role

router = APIRouter(prefix="/api/v1/evaluations", tags=["evaluations"])
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


async def _dataset(session: SessionDep, ctx: WorkspaceDep, dataset_id: uuid.UUID) -> EvaluationDataset:
    d = await session.get(EvaluationDataset, dataset_id)
    if d is None or d.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "dataset not found")
    return d


async def run_out(session: SessionDep, ev: EvaluationRun) -> EvaluationRunOut:
    progress = dict(
        (
            await session.execute(
                select(EvaluationCaseResult.status, func.count())
                .where(EvaluationCaseResult.evaluation_run_id == ev.id)
                .group_by(EvaluationCaseResult.status)
            )
        ).all()
    )
    return EvaluationRunOut.model_validate(ev).model_copy(update={"progress": progress})


@router.post("/datasets", response_model=DatasetOut, status_code=201)
async def create_dataset(body: openeval.Dataset, session: SessionDep, ctx: WorkspaceDep) -> DatasetOut:
    """Import an OpenEval dataset version. Identical re-imports are idempotent; changed content needs a new version."""
    ctx.require(Role.DEVELOPER)
    try:
        row, _ = await import_dataset(session, ctx.workspace_id, body, created_by=ctx.user.id)
    except EvaluationError as exc:
        raise api_error(exc.status, exc.code, str(exc)) from None
    return DatasetOut.model_validate(row)


@router.get("/datasets", response_model=Page[DatasetOut])
async def list_datasets(
    session: SessionDep,
    ctx: WorkspaceDep,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[DatasetOut]:
    q = select(EvaluationDataset).where(EvaluationDataset.workspace_id == ctx.workspace_id)
    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await session.execute(
            q.order_by(EvaluationDataset.name, EvaluationDataset.version.desc(), EvaluationDataset.split)
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    return Page(items=[DatasetOut.model_validate(d) for d in rows], total=total, limit=limit, offset=offset)


@router.get("/datasets/{dataset_id}", response_model=DatasetDetail)
async def get_dataset(dataset_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> DatasetDetail:
    d = await _dataset(session, ctx, dataset_id)
    # Hidden-split answers are restricted: viewers see tasks but not expected outputs.
    hide = d.visibility == "hidden" and ctx.role == Role.VIEWER
    cases = [
        CaseOut(
            case_key=c.case_key,
            task=c.task,
            input=c.input,
            expected_output=None if hide else c.expected_output,
            evaluation_methods=c.evaluator_config["methods"],
            tags=c.tags,
        )
        for c in await dataset_cases(session, d.id)
    ]
    return DatasetDetail(**DatasetOut.model_validate(d).model_dump(), cases=cases, expected_hidden=hide)


@router.post(
    "/run", response_model=EvaluationRunOut, status_code=201, dependencies=[rate_limited("critical")]
)
async def start_run(
    body: EvaluationRunRequest, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.DEVELOPER)
    agent = await session.get(AgentDefinition, body.agent_id)
    if agent is None or agent.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "agent not found")
    dataset = await _dataset(session, ctx, body.dataset_id)
    version = await session.get(AgentConfigVersion, body.agent_version_id or agent.active_version_id)
    if version is None or version.agent_id != agent.id:
        raise api_error(422, "invalid_version", "version does not belong to this agent")
    ws = await session.get(Workspace, ctx.workspace_id)
    policy = load_policy(ws.settings if ws else {})
    if body.trials > policy.max_eval_trials:
        raise api_error(422, "too_many_trials", f"trials must be <= {policy.max_eval_trials}")
    est = estimated_max_cost(dataset.case_count, body.trials, policy.per_case_max_cost_usd)
    paid = AgentConfig.model_validate(version.config_snapshot).provider != "mock"
    if paid and est > policy.paid_confirmation_threshold_usd and not body.confirm_cost:
        raise api_error(
            409,
            "cost_confirmation_required",
            f"this evaluation may cost up to ${est:.2f} with a paid provider; resend with confirm_cost=true",
        )

    async def run() -> EvaluationRunOut:
        try:
            ev = await create_evaluation_run(
                session,
                agent=agent,
                version=version,
                dataset=dataset,
                trials=body.trials,
                created_by=ctx.user.id,
                max_concurrency=policy.max_eval_concurrency,
                per_case_max_cost_usd=policy.per_case_max_cost_usd,
            )
        except EvaluationError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
        await session.flush()
        await session.refresh(ev)
        return await run_out(session, ev)

    return await idempotent(
        session, ctx, scope="evaluation_run", key=idempotency_key, body=body.model_dump(mode="json"), fn=run
    )


@router.get("/runs", response_model=Page[EvaluationRunOut])
async def list_runs(
    session: SessionDep,
    ctx: WorkspaceDep,
    agent_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[EvaluationRunOut]:
    q = select(EvaluationRun).where(EvaluationRun.workspace_id == ctx.workspace_id)
    if agent_id:
        q = q.where(EvaluationRun.agent_id == agent_id)
    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await session.execute(q.order_by(EvaluationRun.created_at.desc()).limit(limit).offset(offset))
    ).scalars()
    return Page(items=[await run_out(session, r) for r in rows], total=total, limit=limit, offset=offset)


async def _run(session: SessionDep, ctx: WorkspaceDep, run_id: uuid.UUID) -> EvaluationRun:
    ev = await session.get(EvaluationRun, run_id)
    if ev is None or ev.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "evaluation run not found")
    return ev


@router.get("/runs/{run_id}", response_model=EvaluationRunOut)
async def get_run(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> EvaluationRunOut:
    return await run_out(session, await _run(session, ctx, run_id))


@router.get("/runs/{run_id}/results")
async def get_results(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    return await evaluation_report(session, await _run(session, ctx, run_id))


@router.get("/runs/{run_id}/export", response_class=PlainTextResponse)
async def export(
    run_id: uuid.UUID,
    session: SessionDep,
    ctx: WorkspaceDep,
    format: str = Query("json", pattern="^(json|csv|md)$"),
) -> PlainTextResponse:
    """Exports always include dataset version/hash, model configuration and methodology."""
    report = await evaluation_report(session, await _run(session, ctx, run_id))
    if format == "csv":
        body, media, ext = openeval.export_csv(report), "text/csv", "csv"
    elif format == "md":
        body, media, ext = openeval.export_markdown(report), "text/markdown", "md"
    else:
        body, media, ext = openeval.export_json(report), "application/json", "json"
    return PlainTextResponse(
        body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="openeval-{run_id}.{ext}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
