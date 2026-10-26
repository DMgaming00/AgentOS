"""Controlled experiments API."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from sqlalchemy import func, select

from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import Page
from agentos.schemas.autopilot import ExperimentOut, ExperimentRequest
from agentos_core.autopilot.evaluation import EvaluationError
from agentos_core.autopilot.experiments import start_experiment
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    OptimizationCandidate,
    OptimizationExperiment,
)
from agentos_core.domain.states import Role

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


async def experiment_out(
    session: SessionDep, e: OptimizationExperiment, *, full: bool = True
) -> ExperimentOut:
    versions = dict(
        (
            await session.execute(
                select(AgentConfigVersion.id, AgentConfigVersion.version).where(
                    AgentConfigVersion.id.in_([e.baseline_version_id, e.candidate_version_id])
                )
            )
        ).all()
    )
    name = (
        await session.execute(select(AgentDefinition.name).where(AgentDefinition.id == e.agent_id))
    ).scalar_one()
    title = None
    if e.candidate_id:
        title = (
            await session.execute(
                select(OptimizationCandidate.title).where(OptimizationCandidate.id == e.candidate_id)
            )
        ).scalar_one_or_none()
    out = ExperimentOut.model_validate(e).model_copy(
        update={
            "agent_name": name,
            "baseline_version": versions.get(e.baseline_version_id),
            "candidate_version": versions.get(e.candidate_version_id),
            "candidate_title": title,
        }
    )
    if not full and out.results:
        # list view: drop bulky per-case arrays
        slim = {k: v for k, v in out.results.items() if k not in ("comparison",)}
        slim["comparison"] = {
            k: v for k, v in (out.results.get("comparison") or {}).items() if k != "per_case"
        }
        out = out.model_copy(update={"results": slim})
    return out


@router.post("", response_model=ExperimentOut, status_code=201, dependencies=[rate_limited("critical")])
async def create(
    body: ExperimentRequest, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.DEVELOPER)

    async def run() -> ExperimentOut:
        try:
            e = await start_experiment(
                session,
                workspace_id=ctx.workspace_id,
                agent_id=body.agent_id,
                dataset_id=body.dataset_id,
                trials=body.trials,
                user_id=ctx.user.id,
                candidate_id=body.candidate_id,
                baseline_version_id=body.baseline_version_id,
                candidate_version_id=body.candidate_version_id,
                confirm_cost=body.confirm_cost,
            )
        except EvaluationError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
        await session.flush()
        await session.refresh(e)
        return await experiment_out(session, e)

    return await idempotent(
        session, ctx, scope="experiment", key=idempotency_key, body=body.model_dump(mode="json"), fn=run
    )


@router.get("", response_model=Page[ExperimentOut])
async def list_experiments(
    session: SessionDep,
    ctx: WorkspaceDep,
    agent_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ExperimentOut]:
    q = select(OptimizationExperiment).where(OptimizationExperiment.workspace_id == ctx.workspace_id)
    if agent_id:
        q = q.where(OptimizationExperiment.agent_id == agent_id)
    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await session.execute(
            q.order_by(OptimizationExperiment.created_at.desc()).limit(limit).offset(offset)
        )
    ).scalars()
    return Page(
        items=[await experiment_out(session, e, full=False) for e in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{experiment_id}", response_model=ExperimentOut)
async def get_experiment(experiment_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> ExperimentOut:
    e = await session.get(OptimizationExperiment, experiment_id)
    if e is None or e.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "experiment not found")
    return await experiment_out(session, e)
