"""Release Center API: environments, gated staged releases, advance/rollback, rules, live evaluation and audit."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos_core.autopilot.config import config_diff
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    AgentEnvironmentPin,
    DeploymentRequest,
    OptimizationExperiment,
    ReleaseEvent,
    ReleasePlan,
)
from agentos_core.domain.states import Role
from agentos_core.release import ALLOWED_PERCENTS, DEFAULT_SCHEDULE, ENVIRONMENTS
from agentos_core.release import controller as rc
from agentos_core.release.gates import blocking, promotion_gates
from agentos_core.release.rules import RollbackRules

router = APIRouter(prefix="/api/v1/releases", tags=["release-center"])
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class _Out(BaseModel):
    model_config = {"extra": "allow"}

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(**data)


class ReleaseIn(BaseModel):
    agent_id: uuid.UUID
    environment: Literal["development", "staging", "production"]
    experiment_id: uuid.UUID | None = None
    candidate_version_id: uuid.UUID | None = None
    schedule: list[int] | None = Field(
        default=None, description=f"increasing, from {list(ALLOWED_PERCENTS)}, ending at 100"
    )
    rules: dict[str, Any] | None = None
    auto_rollback: bool = False
    min_stage_samples: int = Field(default=10, ge=1, le=10_000)
    min_stage_seconds: int = Field(default=0, ge=0, le=86_400)
    seed: str | None = Field(default=None, max_length=64)
    reason: str = Field(default="", max_length=2000)


class RollbackIn(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)


class RulesIn(BaseModel):
    rules: dict[str, Any] | None = None
    auto_rollback: bool | None = None
    min_stage_samples: int | None = Field(default=None, ge=1, le=10_000)
    min_stage_seconds: int | None = Field(default=None, ge=0, le=86_400)


def _err(exc: rc.ReleaseError) -> Any:
    raise api_error(exc.status, exc.code, str(exc), details=exc.details) from None


def _plan(p: ReleasePlan, *, agent: str | None = None) -> dict[str, Any]:
    return {
        "id": str(p.id),
        "agent_id": str(p.agent_id),
        "agent": agent,
        "environment": p.environment,
        "status": p.status,
        "schedule": p.schedule,
        "stage_index": p.stage_index,
        "percent": p.schedule[p.stage_index]
        if p.status in ("active", "pending_approval")
        else (100 if p.status == "completed" else 0),
        "seed": p.seed,
        "auto_rollback": p.auto_rollback,
        "min_stage_samples": p.min_stage_samples,
        "min_stage_seconds": p.min_stage_seconds,
        "candidate_version_id": str(p.candidate_version_id),
        "baseline_version_id": str(p.baseline_version_id),
        "experiment_id": str(p.experiment_id) if p.experiment_id else None,
        "request_id": str(p.request_id) if p.request_id else None,
        "created_at": p.created_at.isoformat(),
        "stage_started_at": p.stage_started_at.isoformat() if p.stage_started_at else None,
        "completed_at": p.completed_at.isoformat() if p.completed_at else None,
        "end_reason": p.end_reason,
        "rules": p.rules,
    }


async def _get(session: SessionDep, ctx: WorkspaceDep, plan_id: uuid.UUID) -> ReleasePlan:
    p = await session.get(ReleasePlan, plan_id)
    if p is None or p.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "release not found")
    return p


@router.get("/defaults")
async def defaults() -> dict[str, Any]:
    return {
        "schedule": DEFAULT_SCHEDULE,
        "allowed_percents": list(ALLOWED_PERCENTS),
        "environments": list(ENVIRONMENTS),
        "rules": RollbackRules().model_dump(),
    }


@router.get("/gates")
async def gate_preview(
    session: SessionDep,
    ctx: WorkspaceDep,
    agent_id: uuid.UUID,
    environment: Literal["staging", "production"] = "production",
    experiment_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    agent = await session.get(AgentDefinition, agent_id)
    if agent is None or agent.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "agent not found")
    exp = await session.get(OptimizationExperiment, experiment_id) if experiment_id else None
    if exp is not None and (exp.workspace_id != ctx.workspace_id or exp.agent_id != agent.id):
        raise api_error(404, "not_found", "experiment not found for this agent")
    gates = await promotion_gates(
        session,
        workspace_id=ctx.workspace_id,
        agent=agent,
        experiment=exp,
        require_approval=environment == "production",
    )
    return {
        "environment": environment,
        "gates": gates,
        "blocking": [g["gate"] for g in blocking(gates)],
        "can_request": not blocking(gates),
    }


@router.get("/environments")
async def environments(
    session: SessionDep, ctx: WorkspaceDep, agent_id: uuid.UUID | None = None
) -> dict[str, Any]:
    q = (
        select(AgentDefinition)
        .where(AgentDefinition.workspace_id == ctx.workspace_id)
        .order_by(AgentDefinition.name)
    )
    if agent_id:
        q = q.where(AgentDefinition.id == agent_id)
    agents = (await session.execute(q)).scalars().all()
    versions = {
        v.id: v
        for v in (
            await session.execute(
                select(AgentConfigVersion).where(AgentConfigVersion.workspace_id == ctx.workspace_id)
            )
        ).scalars()
    }
    pins = {
        (p.agent_id, p.environment): p
        for p in (
            await session.execute(
                select(AgentEnvironmentPin)
                .join(AgentDefinition, AgentDefinition.id == AgentEnvironmentPin.agent_id)
                .where(AgentDefinition.workspace_id == ctx.workspace_id)
            )
        ).scalars()
    }
    live = {
        (p.agent_id, p.environment): p
        for p in (
            await session.execute(
                select(ReleasePlan).where(
                    ReleasePlan.workspace_id == ctx.workspace_id,
                    ReleasePlan.status.in_(["pending_approval", "active"]),
                )
            )
        ).scalars()
    }

    def ver(vid: uuid.UUID | None) -> dict[str, Any] | None:
        v = versions.get(vid) if vid else None
        return {"id": str(v.id), "version": v.version, "config_hash": v.config_hash[:12]} if v else None

    items = []
    for a in agents:
        envs: dict[str, Any] = {
            "production": {
                "label": "production-style local simulation",
                "version": ver(a.active_version_id),
                "canary": ver(a.canary_version_id) if a.canary_version_id else None,
                "canary_percent": a.canary_percent,
                "canary_seed": a.canary_seed,
            }
        }
        for env in ("staging", "development"):
            pin = pins.get((a.id, env))
            envs[env] = {
                "version": ver(pin.version_id) if pin else None,
                "pinned": pin is not None,
                "falls_back_to": None if pin else "production active version",
            }
        for (aid, env), p in live.items():
            if aid == a.id:
                envs[env]["live_release"] = {
                    "id": str(p.id),
                    "status": p.status,
                    "percent": p.schedule[p.stage_index],
                }
        items.append({"agent_id": str(a.id), "agent": a.name, "environments": envs})
    return {
        "items": items,
        "note": "'production' is a production-style local simulation inside this deployment, not a hosted production environment.",
    }


@router.get("")
async def list_releases(
    session: SessionDep,
    ctx: WorkspaceDep,
    agent_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    q = (
        select(ReleasePlan, AgentDefinition.name)
        .join(AgentDefinition, AgentDefinition.id == ReleasePlan.agent_id)
        .where(ReleasePlan.workspace_id == ctx.workspace_id)
        .order_by(ReleasePlan.created_at.desc())
        .limit(limit)
    )
    if agent_id:
        q = q.where(ReleasePlan.agent_id == agent_id)
    if status:
        q = q.where(ReleasePlan.status == status)
    return {"items": [_plan(p, agent=name) for p, name in (await session.execute(q)).all()]}


@router.post("", status_code=201, dependencies=[rate_limited("critical")])
async def create_release(
    body: ReleaseIn, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.DEVELOPER)

    async def go() -> _Out:
        try:
            plan = await rc.create_release(
                session,
                workspace_id=ctx.workspace_id,
                agent_id=body.agent_id,
                user_id=ctx.user.id,
                environment=body.environment,
                experiment_id=body.experiment_id,
                candidate_version_id=body.candidate_version_id,
                schedule=body.schedule,
                rules=body.rules,
                auto_rollback=body.auto_rollback,
                min_stage_samples=body.min_stage_samples,
                min_stage_seconds=body.min_stage_seconds,
                seed=body.seed,
                reason=body.reason,
            )
        except rc.ReleaseError as exc:
            _err(exc)
        except ValueError as exc:
            raise api_error(422, "invalid_rules", str(exc)) from None
        await session.flush()
        return _Out(_plan(plan))

    return await idempotent(
        session, ctx, scope="release:create", key=idempotency_key, body=body.model_dump(mode="json"), fn=go
    )


@router.get("/{plan_id}")
async def get_release(plan_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    p = await _get(session, ctx, plan_id)
    agent = await session.get(AgentDefinition, p.agent_id)
    base = await session.get(AgentConfigVersion, p.baseline_version_id)
    cand = await session.get(AgentConfigVersion, p.candidate_version_id)
    out: dict[str, Any] = _plan(p, agent=agent.name if agent else None)
    out["baseline_version"] = base.version if base else None
    out["candidate_version"] = cand.version if cand else None
    out["config_diff"] = config_diff(base.config_snapshot, cand.config_snapshot) if base and cand else []
    req = await session.get(DeploymentRequest, p.request_id) if p.request_id else None
    out["approval"] = (
        {
            "status": req.status,
            "requested_by": str(req.requested_by) if req.requested_by else None,
            "decided_by": str(req.decided_by) if req.decided_by else None,
            "decided_at": req.decided_at.isoformat() if req.decided_at else None,
            "reason": req.decision_reason,
            "expires_at": req.expires_at.isoformat(),
        }
        if req
        else None
    )
    if p.status == "active" and p.environment == "production":
        ev = await rc.evaluate_plan(session, p)
        out["evaluation"] = ev
        out["stage_gates"] = await rc.stage_gates(session, p, ev)
    events = (
        (
            await session.execute(
                select(ReleaseEvent).where(ReleaseEvent.plan_id == p.id).order_by(ReleaseEvent.id)
            )
        )
        .scalars()
        .all()
    )
    out["events"] = [
        {
            "id": e.id,
            "type": e.event_type,
            "actor": e.actor,
            "payload": e.payload,
            "at": e.created_at.isoformat(),
        }
        for e in events
    ]
    return out


@router.post("/{plan_id}/advance", dependencies=[rate_limited("critical")])
async def advance(plan_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    ctx.require(Role.ADMIN)
    await _get(session, ctx, plan_id)
    try:
        plan = await rc.advance(session, plan_id, user_id=ctx.user.id)
    except rc.ReleaseError as exc:
        _err(exc)
    return _plan(plan)


@router.post("/{plan_id}/rollback", dependencies=[rate_limited("critical")])
async def rollback(
    plan_id: uuid.UUID, body: RollbackIn, session: SessionDep, ctx: WorkspaceDep
) -> dict[str, Any]:
    ctx.require(Role.ADMIN)
    await _get(session, ctx, plan_id)
    try:
        plan = await rc.rollback_release(session, plan_id, user_id=ctx.user.id, reason=body.reason)
    except rc.ReleaseError as exc:
        _err(exc)
    return _plan(plan)


@router.patch("/{plan_id}/rules")
async def update_rules(
    plan_id: uuid.UUID, body: RulesIn, session: SessionDep, ctx: WorkspaceDep
) -> dict[str, Any]:
    ctx.require(Role.ADMIN)
    p = await _get(session, ctx, plan_id)
    if p.status not in ("pending_approval", "active"):
        raise api_error(409, "not_active", f"release is {p.status}")
    try:
        if body.rules is not None:
            p.rules = RollbackRules.model_validate(body.rules).model_dump()
    except ValueError as exc:
        raise api_error(422, "invalid_rules", str(exc)) from None
    for f in ("auto_rollback", "min_stage_samples", "min_stage_seconds"):
        v = getattr(body, f)
        if v is not None:
            setattr(p, f, v)
    session.add(
        ReleaseEvent(
            plan_id=p.id,
            event_type="rules_updated",
            actor=str(ctx.user.id),
            payload={
                "rules": p.rules,
                "auto_rollback": p.auto_rollback,
                "min_stage_samples": p.min_stage_samples,
                "min_stage_seconds": p.min_stage_seconds,
            },
        )
    )
    return _plan(p)


@router.post("/{plan_id}/evaluate", dependencies=[rate_limited("critical")])
async def evaluate_now(plan_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    ctx.require(Role.DEVELOPER)
    p = await _get(session, ctx, plan_id)
    if p.status != "active":
        raise api_error(409, "not_active", f"release is {p.status}")
    ev = await rc.evaluate_plan(session, p)
    return {"evaluation": ev, "stage_gates": await rc.stage_gates(session, p, ev)}
