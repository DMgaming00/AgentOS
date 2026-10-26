from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from agentos.api.runs import run_out
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import (
    Page,
    PlannedRunCreate,
    PlanRequest,
    PlanResponse,
    RunCreate,
    RunOut,
    ValidateRequest,
    ValidateResponse,
    WorkflowCreate,
    WorkflowDetail,
    WorkflowOut,
    WorkflowSummary,
)
from agentos.security import idempotency
from agentos.services.workflows import (
    next_version,
    validate_for_workspace,
    workspace_agents,
    workspace_limits,
)
from agentos_core.autopilot.evaluation import HARNESS_WORKFLOW
from agentos_core.config import get_settings
from agentos_core.db.models import Document, WorkflowDefinition, WorkflowRun
from agentos_core.domain.dag import AgentInfo
from agentos_core.domain.errors import AgentOSError
from agentos_core.domain.states import Role
from agentos_core.engine.core import create_planning_run, materialize_run
from agentos_core.planner import plan_workflow
from agentos_core.providers.base import ModelConfig
from agentos_core.providers.factory import get_provider
from agentos_core.tools.registry import get_registry

router = APIRouter(prefix="/api/v1", tags=["workflows"])

PLANNED_WORKFLOW = "ad-hoc-planned"


def _validation(result: Any) -> ValidateResponse:
    return ValidateResponse(
        valid=result.ok,
        issues=[i.to_dict() for i in result.issues],
        topological_order=result.topological_order,
        levels=result.levels,
        normalized=result.spec.model_dump(mode="json", exclude_none=True)
        if result.spec is not None
        else None,
    )


async def _get(session: SessionDep, ctx: WorkspaceDep, workflow_id: uuid.UUID) -> WorkflowDefinition:
    wf = await session.get(WorkflowDefinition, workflow_id)
    if wf is None or wf.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "workflow not found")
    return wf


@router.get("/workflows", response_model=Page[WorkflowSummary])
async def list_workflows(
    session: SessionDep, ctx: WorkspaceDep, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
) -> Page[WorkflowSummary]:
    """Latest version of each workflow name."""
    latest = (
        select(
            WorkflowDefinition.name, func.max(WorkflowDefinition.version).label("v"), func.count().label("n")
        )
        .where(
            WorkflowDefinition.workspace_id == ctx.workspace_id,
            WorkflowDefinition.name.not_in([PLANNED_WORKFLOW, HARNESS_WORKFLOW]),
        )
        .group_by(WorkflowDefinition.name)
        .subquery()
    )
    total = (await session.execute(select(func.count()).select_from(latest))).scalar_one()
    rows = (
        await session.execute(
            select(WorkflowDefinition, latest.c.n)
            .join(
                latest,
                (latest.c.name == WorkflowDefinition.name) & (latest.c.v == WorkflowDefinition.version),
            )
            .where(WorkflowDefinition.workspace_id == ctx.workspace_id)
            .order_by(WorkflowDefinition.name)
            .limit(limit)
            .offset(offset)
        )
    ).all()
    items = []
    for wf, n in rows:
        last = (
            await session.execute(
                select(WorkflowRun.status, WorkflowRun.created_at)
                .join(WorkflowDefinition, WorkflowDefinition.id == WorkflowRun.workflow_definition_id)
                .where(WorkflowDefinition.name == wf.name, WorkflowRun.workspace_id == ctx.workspace_id)
                .order_by(WorkflowRun.created_at.desc())
                .limit(1)
            )
        ).first()
        items.append(
            WorkflowSummary(
                **WorkflowOut.model_validate(wf).model_dump(),
                version_count=n,
                node_count=len((wf.definition or {}).get("nodes", [])),
                last_run_status=last[0] if last else None,
                last_run_at=last[1] if last else None,
            )
        )
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.post("/workflows", response_model=WorkflowOut, status_code=201)
async def create_workflow(body: WorkflowCreate, session: SessionDep, ctx: WorkspaceDep) -> WorkflowOut:
    """Create a workflow, or a new immutable version if the name already exists."""
    ctx.require(Role.DEVELOPER)
    if body.name in (PLANNED_WORKFLOW, HARNESS_WORKFLOW):
        raise api_error(422, "reserved_name", "this workflow name is reserved")
    result, _ = await validate_for_workspace(session, ctx.workspace_id, body.definition)
    if not result.ok:
        raise api_error(
            422, "invalid_workflow", "; ".join(f"{i.node or '-'}: {i.message}" for i in result.issues)
        )
    assert result.spec is not None
    wf = WorkflowDefinition(
        id=uuid.uuid4(),
        workspace_id=ctx.workspace_id,
        name=body.name,
        description=body.description,
        version=await next_version(session, ctx.workspace_id, body.name),
        definition=result.spec.model_dump(mode="json", exclude_none=True),
        created_by=ctx.user.id,
    )
    session.add(wf)
    await session.flush()
    await session.refresh(wf)
    return WorkflowOut.model_validate(wf)


@router.post("/workflows/validate", response_model=ValidateResponse)
async def validate_workflow(
    body: ValidateRequest, session: SessionDep, ctx: WorkspaceDep
) -> ValidateResponse:
    result, _ = await validate_for_workspace(session, ctx.workspace_id, body.definition)
    return _validation(result)


@router.post("/workflows/plan", response_model=PlanResponse, dependencies=[rate_limited("critical")])
async def plan(body: PlanRequest, session: SessionDep, ctx: WorkspaceDep) -> PlanResponse:
    """Ask the configured planner model for a DAG and validate it. Nothing is saved."""
    ctx.require(Role.DEVELOPER)
    s = get_settings()
    provider_name = body.provider or s.planner_provider
    agents = await workspace_agents(session, ctx.workspace_id)
    docs = (
        await session.execute(
            select(Document.filename, Document.title, Document.content_type)
            .where(Document.workspace_id == ctx.workspace_id)
            .order_by(Document.filename)
        )
    ).all()
    catalog = {
        "agents": [
            {"name": a.name, "description": a.description, "allowed_tools": a.allowed_tool_ids}
            for a in agents
        ],
        "tools": [{"name": t["name"], "description": t["description"]} for t in get_registry().catalog()],
        "documents": [{"filename": f, "title": t, "content_type": c} for f, t, c in docs],
    }
    if provider_name == "mock":
        config = ModelConfig(
            model="mock-planner", max_tokens=8000, extra={"scenario": body.scenario or "planner"}
        )
    else:
        model = s.anthropic_model if provider_name == "anthropic" else (s.openai_model or "")
        config = ModelConfig(model=model, max_tokens=8000, timeout_seconds=s.provider_timeout_seconds)
    try:
        provider = get_provider(provider_name)
        outcome = await plan_workflow(
            task=body.task,
            catalog=catalog,
            agents={a.name: AgentInfo(a.name, frozenset(a.allowed_tool_ids or [])) for a in agents},
            tools=get_registry().infos(),
            limits=await workspace_limits(session, ctx.workspace_id),
            config=config,
            provider=provider,
        )
    except AgentOSError as exc:
        status = 503 if exc.retryable else 422
        raise api_error(status, exc.error_type, exc.message) from None
    result = outcome.result
    return PlanResponse(
        valid=outcome.ok,
        definition=outcome.definition,
        issues=[i.to_dict() for i in (result.issues if result else [])],
        levels=result.levels if result else {},
        attempts=[{"issues": a.issues, "usage": a.usage} for a in outcome.attempts],
        provider=provider_name,
        model=config.model,
        simulated=provider_name == "mock",
    )


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetail)
async def get_workflow(workflow_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> WorkflowDetail:
    wf = await _get(session, ctx, workflow_id)
    result, _ = await validate_for_workspace(session, ctx.workspace_id, wf.definition)
    versions = (
        (
            await session.execute(
                select(WorkflowDefinition.version)
                .where(
                    WorkflowDefinition.workspace_id == ctx.workspace_id, WorkflowDefinition.name == wf.name
                )
                .order_by(WorkflowDefinition.version)
            )
        )
        .scalars()
        .all()
    )
    return WorkflowDetail(
        **WorkflowOut.model_validate(wf).model_dump(), validation=_validation(result), versions=list(versions)
    )


@router.get("/workflows/{workflow_id}/versions", response_model=list[WorkflowOut])
async def workflow_versions(
    workflow_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep
) -> list[WorkflowOut]:
    wf = await _get(session, ctx, workflow_id)
    rows = (
        await session.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.workspace_id == ctx.workspace_id, WorkflowDefinition.name == wf.name)
            .order_by(WorkflowDefinition.version.desc())
        )
    ).scalars()
    return [WorkflowOut.model_validate(w) for w in rows]


async def _idempotent(
    session: SessionDep, ctx: WorkspaceDep, scope: str, key: str | None, body: Any
) -> JSONResponse | None:
    if key is None:
        return None
    rec = await idempotency.lookup(
        session, workspace_id=ctx.workspace_id, user_id=ctx.user.id, scope=scope, key=key, body=body
    )
    if rec is None:
        return None
    return JSONResponse(
        rec.response_body, status_code=rec.response_status, headers={"Idempotent-Replayed": "true"}
    )


@router.post(
    "/workflows/{workflow_id}/runs",
    response_model=RunOut,
    status_code=201,
    dependencies=[rate_limited("critical")],
)
async def start_run(
    workflow_id: uuid.UUID,
    body: RunCreate,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    ctx.require(Role.DEVELOPER)
    key = idempotency.validate_key(idempotency_key)
    scope = f"run:{workflow_id}"
    if (replay := await _idempotent(session, ctx, scope, key, body.model_dump(mode="json"))) is not None:
        return replay
    wf = await _get(session, ctx, workflow_id)
    result, agents = await validate_for_workspace(session, ctx.workspace_id, wf.definition)
    if not result.ok or result.spec is None:
        raise api_error(
            422, "invalid_workflow", "workflow no longer validates (agent or tool permissions changed)"
        )
    run = await materialize_run(
        session,
        workflow=wf,
        spec=result.spec,
        run_input=body.input,
        requested_by=ctx.user.id,
        agents_by_name=agents,
        priority=body.priority,
    )
    await session.flush()
    await session.refresh(run)
    out = await run_out(session, run, wf.name)
    if key:
        await idempotency.store(
            session,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            scope=scope,
            key=key,
            body=body.model_dump(mode="json"),
            status=201,
            response=out.model_dump(mode="json"),
        )
    return out


@router.post("/runs/planned", response_model=RunOut, status_code=201, dependencies=[rate_limited("critical")])
async def start_planned_run(
    body: PlannedRunCreate,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """Create a run whose DAG is planned by the model inside the worker (status: planning)."""
    ctx.require(Role.DEVELOPER)
    key = idempotency.validate_key(idempotency_key)
    if (
        replay := await _idempotent(session, ctx, "planned_run", key, body.model_dump(mode="json"))
    ) is not None:
        return replay
    limits = await workspace_limits(session, ctx.workspace_id)
    if body.max_cost_usd > limits.max_cost_usd:
        raise api_error(
            422, "budget_exceeds_limit", f"max_cost_usd exceeds workspace limit {limits.max_cost_usd}"
        )
    wf = (
        await session.execute(
            select(WorkflowDefinition).where(
                WorkflowDefinition.workspace_id == ctx.workspace_id,
                WorkflowDefinition.name == PLANNED_WORKFLOW,
            )
        )
    ).scalar_one_or_none()
    if wf is None:
        wf = WorkflowDefinition(
            id=uuid.uuid4(),
            workspace_id=ctx.workspace_id,
            name=PLANNED_WORKFLOW,
            version=1,
            description="Runs whose DAG is generated by the planner",
            definition={"mode": "planned"},
        )
        session.add(wf)
        await session.flush()
    run = await create_planning_run(
        session,
        workflow=wf,
        task=body.task,
        run_input=body.input,
        requested_by=ctx.user.id,
        budget={
            "max_model_calls": 40,
            "max_tool_calls": 100,
            "max_cost_usd": body.max_cost_usd,
            "max_concurrency": 4,
            "max_duration_seconds": 3600,
        },
    )
    await session.flush()
    await session.refresh(run)
    out = await run_out(session, run, wf.name)
    if key:
        await idempotency.store(
            session,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            scope="planned_run",
            key=key,
            body=body.model_dump(mode="json"),
            status=201,
            response=out.model_dump(mode="json"),
        )
    return out
