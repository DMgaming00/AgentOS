"""FinOps API: usage analytics, budgets (enforced in code), anomalies, evidence-based suggestions.

Every amount is an *estimate from reported token usage and a versioned price table* (simulated pricing offline).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos_core.autopilot.policy import load_policy
from agentos_core.db.models import AgentDefinition, FinopsAnomaly, FinopsBudget, Workspace
from agentos_core.domain.states import Role
from agentos_core.finops import ESTIMATE_LABEL, analytics
from agentos_core.finops.anomalies import THRESHOLDS, detect_for_workspace
from agentos_core.finops.budgets import utilization
from agentos_core.finops.suggestions import ApprovedModel, suggest
from agentos_core.pricing import get_pricing
from agentos_core.util import utcnow

router = APIRouter(prefix="/api/v1/finops", tags=["finops"])
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class _Out(BaseModel):
    model_config = {"extra": "allow"}

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(**data)


class BudgetIn(BaseModel):
    scope: Literal["workspace", "agent"]
    agent_id: uuid.UUID | None = None
    period: Literal["daily", "monthly"]
    limit_usd: Decimal = Field(ge=Decimal("0.000001"), le=Decimal("1000000"))
    mode: Literal["enforce", "alert"] = "enforce"
    warn_fraction: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)


class BudgetPatch(BaseModel):
    limit_usd: Decimal | None = Field(default=None, ge=Decimal("0.000001"), le=Decimal("1000000"))
    mode: Literal["enforce", "alert"] | None = None
    warn_fraction: Decimal | None = Field(default=None, gt=0, le=1)
    enabled: bool | None = None


@router.get("/summary")
async def get_summary(
    session: SessionDep, ctx: WorkspaceDep, days: int = Query(30, ge=1, le=365)
) -> dict[str, Any]:
    s = await analytics.summary(session, ctx.workspace_id, days=days)
    budgets = (
        (
            await session.execute(
                select(FinopsBudget)
                .where(FinopsBudget.workspace_id == ctx.workspace_id)
                .order_by(FinopsBudget.scope, FinopsBudget.period)
            )
        )
        .scalars()
        .all()
    )
    s["budgets"] = [await utilization(session, b) for b in budgets]
    s["pricing"] = {"version": get_pricing().version, "label": ESTIMATE_LABEL}
    return s


@router.get("/budgets")
async def list_budgets(session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(FinopsBudget)
                .where(FinopsBudget.workspace_id == ctx.workspace_id)
                .order_by(FinopsBudget.scope, FinopsBudget.period)
            )
        )
        .scalars()
        .all()
    )
    names = dict(
        (
            await session.execute(
                select(AgentDefinition.id, AgentDefinition.name).where(
                    AgentDefinition.workspace_id == ctx.workspace_id
                )
            )
        ).all()
    )
    items = []
    for b in rows:
        u = await utilization(session, b)
        items.append({**u, "agent": names.get(b.agent_id) if b.agent_id else None})
    return {"items": items, "label": ESTIMATE_LABEL}


@router.post("/budgets", status_code=201, dependencies=[rate_limited("critical")])
async def create_budget(
    body: BudgetIn, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.ADMIN)
    if (body.scope == "agent") != (body.agent_id is not None):
        raise api_error(
            422, "invalid_budget", "agent budgets need an agent_id; workspace budgets must not have one"
        )
    if body.agent_id is not None:
        agent = await session.get(AgentDefinition, body.agent_id)
        if agent is None or agent.workspace_id != ctx.workspace_id:
            raise api_error(404, "not_found", "agent not found")

    async def go() -> _Out:
        existing = (
            await session.execute(
                select(FinopsBudget.id).where(
                    FinopsBudget.workspace_id == ctx.workspace_id,
                    FinopsBudget.scope == body.scope,
                    FinopsBudget.period == body.period,
                    FinopsBudget.agent_id.is_(None)
                    if body.agent_id is None
                    else FinopsBudget.agent_id == body.agent_id,
                )
            )
        ).first()
        if existing is not None:
            raise api_error(
                409, "budget_exists", "a budget for this scope and period already exists; update it instead"
            )
        b = FinopsBudget(
            id=uuid.uuid4(),
            workspace_id=ctx.workspace_id,
            scope=body.scope,
            agent_id=body.agent_id,
            period=body.period,
            limit_usd=body.limit_usd,
            mode=body.mode,
            warn_fraction=body.warn_fraction,
            created_by=ctx.user.id,
        )
        session.add(b)
        await session.flush()
        return _Out(await utilization(session, b))

    return await idempotent(
        session, ctx, scope="finops:budget", key=idempotency_key, body=body.model_dump(mode="json"), fn=go
    )


async def _budget(session: SessionDep, ctx: WorkspaceDep, budget_id: uuid.UUID) -> FinopsBudget:
    b = await session.get(FinopsBudget, budget_id)
    if b is None or b.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "budget not found")
    return b


@router.patch("/budgets/{budget_id}")
async def update_budget(
    budget_id: uuid.UUID, body: BudgetPatch, session: SessionDep, ctx: WorkspaceDep
) -> dict[str, Any]:
    ctx.require(Role.ADMIN)
    b = await _budget(session, ctx, budget_id)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(b, field, value)
    b.updated_at = utcnow()
    await session.flush()
    return await utilization(session, b)


@router.delete("/budgets/{budget_id}")
async def delete_budget(budget_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    ctx.require(Role.ADMIN)
    b = await _budget(session, ctx, budget_id)
    await session.delete(b)
    return {"deleted": str(budget_id)}


def _anomaly(a: FinopsAnomaly) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "kind": a.kind,
        "severity": a.severity,
        "subject": a.subject,
        "summary": a.summary,
        "evidence": a.evidence,
        "status": a.status,
        "detected_at": a.detected_at.isoformat(),
    }


@router.get("/anomalies")
async def list_anomalies(
    session: SessionDep,
    ctx: WorkspaceDep,
    status: Literal["open", "acknowledged"] | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    q = (
        select(FinopsAnomaly)
        .where(FinopsAnomaly.workspace_id == ctx.workspace_id)
        .order_by(FinopsAnomaly.detected_at.desc())
        .limit(limit)
    )
    if status:
        q = q.where(FinopsAnomaly.status == status)
    return {"items": [_anomaly(a) for a in (await session.execute(q)).scalars()], "thresholds": THRESHOLDS}


@router.post("/anomalies/detect", dependencies=[rate_limited("critical")])
async def detect_now(session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    ctx.require(Role.DEVELOPER)
    found, new = await detect_for_workspace(session, ctx.workspace_id)
    return {"found": len(found), "new": new}


@router.post("/anomalies/{anomaly_id}/acknowledge")
async def acknowledge(anomaly_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    ctx.require(Role.DEVELOPER)
    a = await session.get(FinopsAnomaly, anomaly_id)
    if a is None or a.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "anomaly not found")
    a.status, a.acknowledged_by = "acknowledged", ctx.user.id
    return _anomaly(a)


@router.get("/suggestions")
async def suggestions(
    session: SessionDep, ctx: WorkspaceDep, days: int = Query(30, ge=1, le=365)
) -> dict[str, Any]:
    now = utcnow()
    inp = await analytics.load_inputs(session, ctx.workspace_id, since=now - timedelta(days=days))
    summary = analytics.summarize(inp, now=now, days=days)
    ws = await session.get(Workspace, ctx.workspace_id)
    approved = [
        ApprovedModel(m.provider, m.model) for m in load_policy(ws.settings if ws else {}).allowed_models
    ]
    return {
        "label": ESTIMATE_LABEL,
        "items": suggest(inp, summary, approved_models=approved),
        "note": "A saving is shown only as 'measured' (actually spent on something avoidable) or 'projected' (with its formula). "
        "Nothing here is applied automatically.",
    }
