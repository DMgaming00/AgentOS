from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from agentos.api.runs import approvals_out
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import ApprovalOut, DecisionRequest, Page
from agentos.security import idempotency
from agentos_core.db.models import ApprovalRequest
from agentos_core.domain.states import ApprovalStatus, Role
from agentos_core.engine.core import EngineError, resolve_approval

router = APIRouter(prefix="/api/v1/approvals", tags=["approvals"])


@router.get("", response_model=Page[ApprovalOut])
async def list_approvals(
    session: SessionDep,
    ctx: WorkspaceDep,
    status: str | None = Query(None, description="comma-separated"),
    run_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[ApprovalOut]:
    base = select(ApprovalRequest).where(ApprovalRequest.workspace_id == ctx.workspace_id)
    if status:
        base = base.where(ApprovalRequest.status.in_(status.split(",")))
    if run_id:
        base = base.where(ApprovalRequest.run_id == run_id)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    items = await approvals_out(
        session, base.order_by(ApprovalRequest.requested_at.desc()).limit(limit).offset(offset)
    )
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/{approval_id}", response_model=ApprovalOut)
async def get_approval(approval_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> ApprovalOut:
    items = await approvals_out(
        session,
        select(ApprovalRequest).where(
            ApprovalRequest.id == approval_id, ApprovalRequest.workspace_id == ctx.workspace_id
        ),
    )
    if not items:
        raise api_error(404, "not_found", "approval not found")
    return items[0]


async def _decide(
    approval_id: uuid.UUID,
    decision: ApprovalStatus,
    body: DecisionRequest,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: str | None,
) -> Any:
    # Approving side effects requires developer or admin; viewers can only look.
    ctx.require(Role.DEVELOPER)
    key = idempotency.validate_key(idempotency_key)
    scope = f"approval:{approval_id}"
    payload = {"decision": decision.value, **body.model_dump(mode="json")}
    if key:
        rec = await idempotency.lookup(
            session, workspace_id=ctx.workspace_id, user_id=ctx.user.id, scope=scope, key=key, body=payload
        )
        if rec is not None:
            return JSONResponse(
                rec.response_body, status_code=rec.response_status, headers={"Idempotent-Replayed": "true"}
            )
    owner = (
        await session.execute(select(ApprovalRequest.workspace_id).where(ApprovalRequest.id == approval_id))
    ).scalar_one_or_none()
    if owner != ctx.workspace_id:
        raise api_error(404, "not_found", "approval not found")
    try:
        approval = await resolve_approval(
            session, approval_id, decision=decision, user_id=ctx.user.id, reason=body.reason
        )
    except EngineError as exc:
        raise api_error(exc.status_code, exc.code, str(exc)) from None
    await session.flush()
    (out,) = await approvals_out(session, select(ApprovalRequest).where(ApprovalRequest.id == approval.id))
    if key:
        await idempotency.store(
            session,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            scope=scope,
            key=key,
            body=payload,
            status=200,
            response=out.model_dump(mode="json"),
        )
    if approval.status != decision.value:
        # e.g. expired before the decision arrived: report it, the state change is still committed
        return JSONResponse(out.model_dump(mode="json"), status_code=409)
    return out


@router.post("/{approval_id}/approve", response_model=ApprovalOut, dependencies=[rate_limited("critical")])
async def approve(
    approval_id: uuid.UUID,
    body: DecisionRequest,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    return await _decide(approval_id, ApprovalStatus.APPROVED, body, session, ctx, idempotency_key)


@router.post("/{approval_id}/reject", response_model=ApprovalOut, dependencies=[rate_limited("critical")])
async def reject(
    approval_id: uuid.UUID,
    body: DecisionRequest,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    return await _decide(approval_id, ApprovalStatus.REJECTED, body, session, ctx, idempotency_key)
