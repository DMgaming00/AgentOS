from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import select

from agentos.dependencies import SessionDep, UserDep, WorkspaceDep
from agentos.schemas.api import SystemInfo, WorkspaceOut, WorkspaceUpdate
from agentos_core.config import get_settings
from agentos_core.db.models import User, Workspace, WorkspaceMember
from agentos_core.domain.states import Role
from agentos_core.notify import get_notifier
from agentos_core.pricing import get_pricing
from agentos_core.providers.execution import execution_status
from agentos_core.providers.factory import provider_status

router = APIRouter(prefix="/api/v1", tags=["workspace"])
VERSION = "0.1.0"


async def _workspace_out(session: SessionDep, ctx: WorkspaceDep) -> WorkspaceOut:
    ws = await session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    members = (
        await session.execute(
            select(User.email, WorkspaceMember.role)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == ws.id)
            .order_by(User.email)
        )
    ).all()
    return WorkspaceOut.model_validate(ws).model_copy(
        update={"role": ctx.role.value, "members": [{"email": e, "role": r} for e, r in members]}
    )


@router.get("/workspace", response_model=WorkspaceOut)
async def get_workspace(session: SessionDep, ctx: WorkspaceDep) -> WorkspaceOut:
    return await _workspace_out(session, ctx)


@router.patch("/workspace", response_model=WorkspaceOut)
async def update_workspace(body: WorkspaceUpdate, session: SessionDep, ctx: WorkspaceDep) -> WorkspaceOut:
    ctx.require(Role.ADMIN)
    ws = await session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    if body.name is not None:
        ws.name = body.name
    if body.max_run_cost_usd is not None:
        ws.settings = {**(ws.settings or {}), "max_run_cost_usd": body.max_run_cost_usd}
    await session.flush()
    return await _workspace_out(session, ctx)


@router.get("/system", response_model=SystemInfo)
async def system_info(_: UserDep) -> SystemInfo:
    """Non-secret runtime information. Never includes key values."""
    s = get_settings()
    pricing = get_pricing()
    return SystemInfo(
        version=VERSION,
        environment=s.environment,
        providers=provider_status(),
        execution=execution_status(),
        planner_provider=s.planner_provider,
        dev_auth_enabled=s.dev_auth_enabled,
        otel_enabled=s.otel_enabled,
        redis_available=await get_notifier().ping(),
        pricing={"version": pricing.version, "source": pricing.source, "models": sorted(pricing.prices)},
        limits={
            "max_dag_nodes": s.max_dag_nodes,
            "max_run_cost_usd": s.max_run_cost_usd,
            "max_upload_bytes": s.max_upload_bytes,
            "max_artifact_bytes": s.max_artifact_bytes,
            "approval_ttl_seconds": s.approval_ttl_seconds,
        },
        engine={
            "lease_ttl_seconds": s.lease_ttl_seconds,
            "heartbeat_interval_seconds": s.heartbeat_interval_seconds,
            "worker_concurrency": s.worker_concurrency,
            "delivery": "at-least-once with idempotent side effects",
        },
    )
