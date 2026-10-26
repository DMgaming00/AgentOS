from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import (
    ApprovalOut,
    AttemptOut,
    EventOut,
    ModelUsageOut,
    Page,
    PauseRequest,
    RunOut,
    StepDetail,
    StepOut,
    ToolExecutionOut,
)
from agentos_core.db.models import (
    AgentDefinition,
    ApprovalRequest,
    Event,
    ModelUsage,
    StepAttempt,
    ToolExecution,
    User,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.domain.states import Role
from agentos_core.engine.core import EngineError, cancel_run, pause_run, resume_run
from agentos_core.util import utcnow

router = APIRouter(prefix="/api/v1/runs", tags=["runs"])


async def run_out(session: AsyncSession, run: WorkflowRun, workflow_name: str | None = None) -> RunOut:
    counts: dict[str, int] = dict(
        (
            await session.execute(
                select(WorkflowStep.status, func.count())
                .where(WorkflowStep.run_id == run.id)
                .group_by(WorkflowStep.status)
            )
        ).all()
    )
    if workflow_name is None:
        workflow_name = (
            await session.execute(
                select(WorkflowDefinition.name).where(WorkflowDefinition.id == run.workflow_definition_id)
            )
        ).scalar_one_or_none()
    duration = None
    if run.started_at:
        duration = int(((run.completed_at or utcnow()) - run.started_at).total_seconds() * 1000)
    return RunOut.model_validate(run).model_copy(
        update={"workflow_name": workflow_name, "step_counts": counts, "duration_ms": duration}
    )


async def get_run_for(session: AsyncSession, ctx: WorkspaceDep, run_id: uuid.UUID) -> WorkflowRun:
    run = await session.get(WorkflowRun, run_id)
    if run is None or run.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "run not found")
    return run


@router.get("", response_model=Page[RunOut])
async def list_runs(
    session: SessionDep,
    ctx: WorkspaceDep,
    status: str | None = None,
    workflow_id: uuid.UUID | None = None,
    purpose: str | None = Query(None, pattern="^(workflow|evaluation|replay)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[RunOut]:
    base = select(WorkflowRun).where(WorkflowRun.workspace_id == ctx.workspace_id)
    if status:
        base = base.where(WorkflowRun.status.in_(status.split(",")))
    if workflow_id:
        base = base.where(WorkflowRun.workflow_definition_id == workflow_id)
    if purpose:
        base = base.where(WorkflowRun.purpose == purpose)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(base.order_by(WorkflowRun.created_at.desc()).limit(limit).offset(offset))
    ).scalars()
    return Page(items=[await run_out(session, r) for r in rows], total=total, limit=limit, offset=offset)


@router.get("/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> RunOut:
    return await run_out(session, await get_run_for(session, ctx, run_id))


@router.post("/{run_id}/cancel", response_model=RunOut, dependencies=[rate_limited("critical")])
async def cancel(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> RunOut:
    ctx.require(Role.DEVELOPER)
    await get_run_for(session, ctx, run_id)
    try:
        run = await cancel_run(session, run_id, user_id=ctx.user.id)
    except EngineError as exc:
        raise api_error(exc.status_code, exc.code, str(exc)) from None
    await session.flush()
    await session.refresh(run)
    return await run_out(session, run)


@router.post("/{run_id}/pause", response_model=RunOut, dependencies=[rate_limited("critical")])
async def pause(
    run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep, body: PauseRequest | None = None
) -> RunOut:
    """Stop claiming new steps for this run and its child runs. Running steps finish; nothing is lost."""
    ctx.require(Role.DEVELOPER)
    await get_run_for(session, ctx, run_id)
    try:
        run = await pause_run(session, run_id, user_id=ctx.user.id, reason=body.reason if body else None)
    except EngineError as exc:
        raise api_error(exc.status_code, exc.code, str(exc)) from None
    await session.flush()
    await session.refresh(run)
    return await run_out(session, run)


@router.post("/{run_id}/resume", response_model=RunOut, dependencies=[rate_limited("critical")])
async def resume(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> RunOut:
    ctx.require(Role.DEVELOPER)
    await get_run_for(session, ctx, run_id)
    try:
        run = await resume_run(session, run_id, user_id=ctx.user.id)
    except EngineError as exc:
        raise api_error(exc.status_code, exc.code, str(exc)) from None
    await session.flush()
    await session.refresh(run)
    return await run_out(session, run)


async def _attempts(session: AsyncSession, step_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[AttemptOut]]:
    out: dict[uuid.UUID, list[AttemptOut]] = {sid: [] for sid in step_ids}
    rows = (
        await session.execute(
            select(StepAttempt).where(StepAttempt.step_id.in_(step_ids)).order_by(StepAttempt.attempt_number)
        )
    ).scalars()
    for a in rows:
        out[a.step_id].append(AttemptOut.model_validate(a))
    return out


@router.get("/{run_id}/steps", response_model=list[StepOut])
async def list_steps(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> list[StepOut]:
    await get_run_for(session, ctx, run_id)
    rows = (
        await session.execute(
            select(WorkflowStep, AgentDefinition.name)
            .outerjoin(AgentDefinition, AgentDefinition.id == WorkflowStep.agent_definition_id)
            .where(WorkflowStep.run_id == run_id)
            .order_by(WorkflowStep.created_at, WorkflowStep.node_key)
        )
    ).all()
    attempts = await _attempts(session, [s.id for s, _ in rows])
    return [
        StepOut.model_validate(s).model_copy(update={"agent_name": n, "attempts": attempts[s.id]})
        for s, n in rows
    ]


@router.get("/{run_id}/steps/{step_id}", response_model=StepDetail)
async def step_detail(
    run_id: uuid.UUID, step_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep
) -> StepDetail:
    await get_run_for(session, ctx, run_id)
    step = await session.get(WorkflowStep, step_id)
    if step is None or step.run_id != run_id:
        raise api_error(404, "not_found", "step not found")
    agent_name = None
    if step.agent_definition_id:
        agent_name = (
            await session.execute(
                select(AgentDefinition.name).where(AgentDefinition.id == step.agent_definition_id)
            )
        ).scalar_one_or_none()
    tes = (
        await session.execute(
            select(ToolExecution).where(ToolExecution.step_id == step_id).order_by(ToolExecution.created_at)
        )
    ).scalars()
    usage = (
        await session.execute(
            select(ModelUsage).where(ModelUsage.step_id == step_id).order_by(ModelUsage.recorded_at)
        )
    ).scalars()
    events = (
        await session.execute(select(Event).where(Event.step_id == step_id).order_by(Event.sequence_number))
    ).scalars()
    approvals = await approvals_out(
        session,
        select(ApprovalRequest)
        .where(ApprovalRequest.step_id == step_id)
        .order_by(ApprovalRequest.requested_at),
    )
    return StepDetail(
        **StepOut.model_validate(step).model_dump(exclude={"attempts", "agent_name"}),
        agent_name=agent_name,
        attempts=(await _attempts(session, [step_id]))[step_id],
        tool_executions=[ToolExecutionOut.model_validate(t) for t in tes],
        model_usage=[ModelUsageOut.model_validate(u) for u in usage],
        events=[EventOut.model_validate(e) for e in events],
        approvals=approvals,
    )


async def approvals_out(session: AsyncSession, stmt) -> list[ApprovalOut]:  # type: ignore[no-untyped-def]
    rows = (
        await session.execute(
            stmt.add_columns(
                WorkflowStep.node_key, WorkflowDefinition.name, ToolExecution.tool_name, User.email
            )
            .join(WorkflowStep, WorkflowStep.id == ApprovalRequest.step_id)
            .join(WorkflowRun, WorkflowRun.id == ApprovalRequest.run_id)
            .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
            .outerjoin(ToolExecution, ToolExecution.id == ApprovalRequest.tool_execution_id)
            .outerjoin(User, User.id == ApprovalRequest.decided_by)
        )
    ).all()
    return [
        ApprovalOut.model_validate(a).model_copy(
            update={"node_key": nk, "workflow_name": wn, "tool_name": tn, "decided_by_email": email}
        )
        for a, nk, wn, tn, email in rows
    ]


@router.get("/{run_id}/tool-executions", response_model=list[ToolExecutionOut])
async def run_tool_executions(
    run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep
) -> list[ToolExecutionOut]:
    await get_run_for(session, ctx, run_id)
    rows = (
        await session.execute(
            select(ToolExecution).where(ToolExecution.run_id == run_id).order_by(ToolExecution.created_at)
        )
    ).scalars()
    return [ToolExecutionOut.model_validate(t) for t in rows]


@router.get("/{run_id}/usage", response_model=list[ModelUsageOut])
async def run_usage(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> list[ModelUsageOut]:
    await get_run_for(session, ctx, run_id)
    rows = (
        await session.execute(
            select(ModelUsage).where(ModelUsage.run_id == run_id).order_by(ModelUsage.recorded_at)
        )
    ).scalars()
    return [ModelUsageOut.model_validate(u) for u in rows]
