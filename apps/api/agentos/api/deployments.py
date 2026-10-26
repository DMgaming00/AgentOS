"""Safe deployment API: promotion requests, admin decisions, rollback, history, health."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import Page
from agentos.schemas.autopilot import (
    DecisionIn,
    DeploymentOut,
    DeploymentRequestOut,
    PromoteRequest,
    RollbackRequest,
)
from agentos_core.autopilot import deployment as dep
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    AgentDeployment,
    DeploymentRequest,
    RolloutEvent,
    User,
)
from agentos_core.domain.states import Role

router = APIRouter(prefix="/api/v1/deployments", tags=["deployments"])
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


async def _versions(session: SessionDep, ids: list[uuid.UUID | None]) -> dict[uuid.UUID, int]:
    clean = [i for i in ids if i]
    if not clean:
        return {}
    return dict(
        (
            await session.execute(
                select(AgentConfigVersion.id, AgentConfigVersion.version).where(
                    AgentConfigVersion.id.in_(clean)
                )
            )
        ).all()
    )


async def request_out(session: SessionDep, r: DeploymentRequest) -> DeploymentRequestOut:
    v = await _versions(session, [r.candidate_version_id, r.baseline_version_id])
    name = (
        await session.execute(select(AgentDefinition.name).where(AgentDefinition.id == r.agent_id))
    ).scalar_one()
    return DeploymentRequestOut.model_validate(r).model_copy(
        update={
            "agent_name": name,
            "candidate_version": v.get(r.candidate_version_id),
            "baseline_version": v.get(r.baseline_version_id),
        }
    )


async def deployment_out(session: SessionDep, d: AgentDeployment) -> DeploymentOut:
    v = await _versions(session, [d.config_version_id, d.previous_version_id])
    name = (
        await session.execute(select(AgentDefinition.name).where(AgentDefinition.id == d.agent_id))
    ).scalar_one()
    email = (
        (await session.execute(select(User.email).where(User.id == d.approved_by))).scalar_one_or_none()
        if d.approved_by
        else None
    )
    events = (
        await session.execute(
            select(RolloutEvent).where(RolloutEvent.deployment_id == d.id).order_by(RolloutEvent.id)
        )
    ).scalars()
    return DeploymentOut.model_validate(d).model_copy(
        update={
            "agent_name": name,
            "version": v.get(d.config_version_id),
            "previous_version": v.get(d.previous_version_id) if d.previous_version_id else None,
            "approved_by_email": email,
            "rollout_events": [
                {
                    "id": e.id,
                    "type": e.event_type,
                    "payload": e.payload,
                    "created_at": e.created_at.isoformat(),
                }
                for e in events
            ],
        }
    )


def _err(exc: dep.DeploymentError) -> Exception:
    return api_error(exc.status, exc.code, str(exc))


@router.post(
    "/promote", response_model=DeploymentRequestOut, status_code=201, dependencies=[rate_limited("critical")]
)
async def promote(
    body: PromoteRequest, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    """Request promotion of an eligible experiment's candidate. An administrator must approve it."""
    ctx.require(Role.DEVELOPER)

    async def run() -> DeploymentRequestOut:
        try:
            r = await dep.request_promotion(
                session,
                workspace_id=ctx.workspace_id,
                experiment_id=body.experiment_id,
                user_id=ctx.user.id,
                mode=body.mode,
                canary_percent=body.canary_percent,
                reason=body.reason,
            )
        except dep.DeploymentError as exc:
            raise _err(exc) from None
        await session.flush()
        return await request_out(session, r)

    return await idempotent(
        session, ctx, scope="promote", key=idempotency_key, body=body.model_dump(mode="json"), fn=run
    )


@router.get("/requests", response_model=Page[DeploymentRequestOut])
async def list_requests(
    session: SessionDep,
    ctx: WorkspaceDep,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[DeploymentRequestOut]:
    q = select(DeploymentRequest).where(DeploymentRequest.workspace_id == ctx.workspace_id)
    if status:
        q = q.where(DeploymentRequest.status.in_(status.split(",")))
    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await session.execute(q.order_by(DeploymentRequest.requested_at.desc()).limit(limit).offset(offset))
    ).scalars()
    return Page(items=[await request_out(session, r) for r in rows], total=total, limit=limit, offset=offset)


async def _decide(
    request_id: uuid.UUID,
    approve: bool,
    body: DecisionIn,
    session: SessionDep,
    ctx: WorkspaceDep,
    key: str | None,
) -> Any:
    ctx.require(Role.ADMIN)  # promotions require an administrator

    async def run() -> DeploymentRequestOut:
        try:
            r = await dep.decide_request(
                session,
                workspace_id=ctx.workspace_id,
                request_id=request_id,
                approve=approve,
                user_id=ctx.user.id,
                reason=body.reason,
            )
        except dep.DeploymentError as exc:
            raise _err(exc) from None
        await session.flush()
        return await request_out(session, r)

    resp = await idempotent(
        session,
        ctx,
        scope=f"deploy_decision:{request_id}",
        key=key,
        body={"approve": approve, **body.model_dump(mode="json")},
        fn=run,
        status=200,
    )
    if isinstance(resp, JSONResponse) and approve:
        import json

        status = json.loads(bytes(resp.body)).get("status")
        if status in ("expired", "rejected"):  # committed, but the approval did not take effect
            return JSONResponse(json.loads(bytes(resp.body)), status_code=409)
    return resp


@router.post("/requests/{request_id}/approve", response_model=DeploymentRequestOut)
async def approve(
    request_id: uuid.UUID,
    body: DecisionIn,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: IdemKey = None,
) -> Any:
    return await _decide(request_id, True, body, session, ctx, idempotency_key)


@router.post("/requests/{request_id}/reject", response_model=DeploymentRequestOut)
async def reject(
    request_id: uuid.UUID,
    body: DecisionIn,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: IdemKey = None,
) -> Any:
    return await _decide(request_id, False, body, session, ctx, idempotency_key)


@router.post(
    "/rollback", response_model=DeploymentOut, status_code=201, dependencies=[rate_limited("critical")]
)
async def rollback(
    body: RollbackRequest, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.ADMIN)

    async def run() -> DeploymentOut:
        try:
            d = await dep.rollback(
                session,
                workspace_id=ctx.workspace_id,
                agent_id=body.agent_id,
                user_id=ctx.user.id,
                reason=body.reason,
                target_version_id=body.target_version_id,
            )
        except dep.DeploymentError as exc:
            raise _err(exc) from None
        await session.flush()
        return await deployment_out(session, d)

    return await idempotent(
        session, ctx, scope="rollback", key=idempotency_key, body=body.model_dump(mode="json"), fn=run
    )


@router.get("/history", response_model=Page[DeploymentOut])
async def history(
    session: SessionDep,
    ctx: WorkspaceDep,
    agent_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[DeploymentOut]:
    q = select(AgentDeployment).where(AgentDeployment.workspace_id == ctx.workspace_id)
    if agent_id:
        q = q.where(AgentDeployment.agent_id == agent_id)
    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await session.execute(q.order_by(AgentDeployment.deployed_at.desc()).limit(limit).offset(offset))
    ).scalars()
    return Page(
        items=[await deployment_out(session, d) for d in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{deployment_id}/health")
async def health(deployment_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    d = await session.get(AgentDeployment, deployment_id)
    if d is None or d.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "deployment not found")
    return await dep.deployment_health(session, d)
