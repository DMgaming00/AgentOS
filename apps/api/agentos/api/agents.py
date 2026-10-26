from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from agentos.dependencies import SessionDep, WorkspaceDep, api_error
from agentos.schemas.api import AgentCreate, AgentExecution, AgentOut, AgentUpdate, AgentVersionOut, Page
from agentos_core.autopilot.versions import apply_manual_edit, initialize_agent_versioning, lock_agent
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.domain.states import Role
from agentos_core.tools.registry import get_registry

router = APIRouter(prefix="/api/v1/agents", tags=["agents"])


def _check_tools(tools: list[str]) -> None:
    unknown = sorted(set(tools) - set(get_registry().names()))
    if unknown:
        raise api_error(422, "unknown_tool", f"unknown tools: {', '.join(unknown)}")


async def _get(session: SessionDep, ctx: WorkspaceDep, agent_id: uuid.UUID) -> AgentDefinition:
    agent = await session.get(AgentDefinition, agent_id)
    if agent is None or agent.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "agent not found")
    return agent


@router.get("", response_model=Page[AgentOut])
async def list_agents(
    session: SessionDep, ctx: WorkspaceDep, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
) -> Page[AgentOut]:
    base = select(AgentDefinition).where(AgentDefinition.workspace_id == ctx.workspace_id)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (await session.execute(base.order_by(AgentDefinition.name).limit(limit).offset(offset))).scalars()
    return Page(items=[AgentOut.model_validate(a) for a in rows], total=total, limit=limit, offset=offset)


@router.post("", response_model=AgentOut, status_code=201)
async def create_agent(body: AgentCreate, session: SessionDep, ctx: WorkspaceDep) -> AgentOut:
    ctx.require(Role.DEVELOPER)
    _check_tools(body.allowed_tool_ids)
    agent = AgentDefinition(id=uuid.uuid4(), workspace_id=ctx.workspace_id, **body.model_dump())
    session.add(agent)
    try:
        await session.flush()
    except IntegrityError:
        raise api_error(409, "conflict", f"an agent named {body.name!r} already exists") from None
    await initialize_agent_versioning(session, agent, created_by=ctx.user.id)
    await session.refresh(agent)
    return AgentOut.model_validate(agent)


@router.get("/{agent_id}", response_model=AgentOut)
async def get_agent(agent_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> AgentOut:
    return AgentOut.model_validate(await _get(session, ctx, agent_id))


@router.patch("/{agent_id}", response_model=AgentOut)
async def update_agent(
    agent_id: uuid.UUID, body: AgentUpdate, session: SessionDep, ctx: WorkspaceDep
) -> AgentOut:
    ctx.require(Role.DEVELOPER)
    agent = await _get(session, ctx, agent_id)
    changes = body.model_dump(exclude_unset=True)
    if "allowed_tool_ids" in changes:
        _check_tools(changes["allowed_tool_ids"])
    agent = await lock_agent(session, agent.id)
    # Identity fields are edited in place; behavior fields create (or reuse) an immutable version.
    for k in ("name", "description"):
        if k in changes:
            setattr(agent, k, changes.pop(k))
    if changes:
        try:
            await apply_manual_edit(session, agent, changes, user_id=ctx.user.id)
        except ValidationError as exc:
            raise api_error(422, "invalid_config", str(exc.errors(include_url=False)[0]["msg"])) from None
    try:
        await session.flush()
    except IntegrityError:
        raise api_error(409, "conflict", "an agent with that name already exists") from None
    await session.refresh(agent)
    return AgentOut.model_validate(agent)


@router.get("/{agent_id}/executions", response_model=Page[AgentExecution])
async def agent_executions(
    agent_id: uuid.UUID,
    session: SessionDep,
    ctx: WorkspaceDep,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[AgentExecution]:
    await _get(session, ctx, agent_id)
    base = (
        select(WorkflowStep, WorkflowDefinition.name)
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
        .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
        .where(WorkflowStep.agent_definition_id == agent_id, WorkflowRun.workspace_id == ctx.workspace_id)
    )
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(base.order_by(WorkflowStep.created_at.desc()).limit(limit).offset(offset))
    ).all()
    items = [
        AgentExecution(
            run_id=s.run_id,
            step_id=s.id,
            node_key=s.node_key,
            status=s.status,
            attempt_count=s.attempt_count,
            started_at=s.started_at,
            completed_at=s.completed_at,
            workflow_name=name,
        )
        for s, name in rows
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{agent_id}/versions", response_model=list[AgentVersionOut])
async def list_versions(agent_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> list[AgentVersionOut]:
    """Immutable version history, newest first, with which version is active / canary."""
    agent = await _get(session, ctx, agent_id)
    rows = (
        await session.execute(
            select(AgentConfigVersion)
            .where(AgentConfigVersion.agent_id == agent_id)
            .order_by(AgentConfigVersion.version.desc())
        )
    ).scalars()
    return [_version_out(v, agent) for v in rows]


@router.get("/{agent_id}/versions/{version_id}", response_model=AgentVersionOut)
async def get_version(
    agent_id: uuid.UUID, version_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep
) -> AgentVersionOut:
    agent = await _get(session, ctx, agent_id)
    v = await session.get(AgentConfigVersion, version_id)
    if v is None or v.agent_id != agent_id:
        raise api_error(404, "not_found", "version not found")
    return _version_out(v, agent)


def _version_out(v: AgentConfigVersion, agent: AgentDefinition) -> AgentVersionOut:
    return AgentVersionOut.model_validate(v).model_copy(
        update={"is_active": v.id == agent.active_version_id, "is_canary": v.id == agent.canary_version_id}
    )
