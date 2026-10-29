"""Agent Security Center API: run the suite, read results, catalog and incidents."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel
from sqlalchemy import func, select

from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos_core.db.models import SecurityEvaluationResult, SecurityEvaluationRun, SecurityTestCase
from agentos_core.domain.states import Role
from agentos_core.engine.core import EngineError
from agentos_core.security.corpus import SUITE_VERSION
from agentos_core.security.incidents import SOURCES, list_incidents
from agentos_core.security.runner import seed_catalog, start_security_run

router = APIRouter(prefix="/api/v1/security", tags=["security-center"])


class _Out(BaseModel):
    model_config = {"extra": "allow"}

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(**data)


def _run_summary(r: SecurityEvaluationRun) -> dict[str, Any]:
    m = (r.results or {}).get("metrics", {})
    return {
        "id": str(r.id),
        "suite_version": r.suite_version,
        "status": r.status,
        "phase": r.phase,
        "created_at": r.created_at.isoformat(),
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "counts": (r.results or {}).get("counts"),
        "error": r.error,
        "agent_mode": (r.conditions or {}).get("agent_mode", "mock"),
        "agent_provider": (r.conditions or {}).get("agent_provider"),
        "successful_policy_enforcement": (m.get("successful_policy_enforcement") or {}).get("value"),
        "unsafe_side_effects": m.get("unsafe_side_effects"),
    }


async def _get_run(session: Any, ctx: Any, run_id: uuid.UUID) -> SecurityEvaluationRun:
    r: SecurityEvaluationRun | None = await session.get(SecurityEvaluationRun, run_id)
    if r is None or r.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "security evaluation not found")
    return r


@router.get("/overview")
async def overview(session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    latest = (
        await session.execute(
            select(SecurityEvaluationRun)
            .where(
                SecurityEvaluationRun.workspace_id == ctx.workspace_id,
                SecurityEvaluationRun.status == "completed",
            )
            .order_by(SecurityEvaluationRun.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    running = (
        await session.execute(
            select(SecurityEvaluationRun).where(
                SecurityEvaluationRun.workspace_id == ctx.workspace_id,
                SecurityEvaluationRun.status == "running",
            )
        )
    ).scalar_one_or_none()
    incidents = await list_incidents(session, ctx.workspace_id, days=30, limit=500)
    by_source = {s: sum(1 for i in incidents if i["source"] == s) for s in SOURCES}
    return {
        "suite_version": SUITE_VERSION,
        "latest": (_run_summary(latest) | {"results": latest.results}) if latest else None,
        "running": _run_summary(running) if running else None,
        "incidents_30d": by_source,
        "incident_total_30d": len(incidents),
        "recent_incidents": incidents[:8],
    }


@router.post("/runs", status_code=201, dependencies=[rate_limited("critical")])
async def start(
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    agent: Annotated[
        Literal["mock", "live", "live_naive"],
        Query(description="'live' uses a real model; needs paid requests enabled"),
    ] = "mock",
) -> Any:
    ctx.require(Role.DEVELOPER)
    if agent != "mock":
        ctx.require(Role.ADMIN)  # spends money

    async def go() -> Any:
        try:
            run = await start_security_run(
                session, workspace_id=ctx.workspace_id, user_id=ctx.user.id, agent=agent
            )
        except EngineError as exc:
            raise api_error(exc.status_code, exc.code, str(exc)) from None
        await session.flush()
        await session.refresh(run)
        return _Out(_run_summary(run))

    return await idempotent(
        session, ctx, scope="security:start", key=idempotency_key, body={"agent": agent}, fn=go
    )


@router.get("/runs")
async def runs(
    session: SessionDep, ctx: WorkspaceDep, limit: int = Query(20, ge=1, le=100)
) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(SecurityEvaluationRun)
                .where(SecurityEvaluationRun.workspace_id == ctx.workspace_id)
                .order_by(SecurityEvaluationRun.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_run_summary(r) for r in rows]}


@router.get("/runs/{run_id}")
async def run_detail(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    r = await _get_run(session, ctx, run_id)
    titles = {
        c.case_key: c.title
        for c in (
            await session.execute(
                select(SecurityTestCase).where(SecurityTestCase.suite_version == r.suite_version)
            )
        ).scalars()
    }
    expected = {
        c.case_key: c.expected
        for c in (
            await session.execute(
                select(SecurityTestCase).where(SecurityTestCase.suite_version == r.suite_version)
            )
        ).scalars()
    }
    results = (
        (
            await session.execute(
                select(SecurityEvaluationResult)
                .where(SecurityEvaluationResult.evaluation_run_id == r.id)
                .order_by(SecurityEvaluationResult.kind, SecurityEvaluationResult.case_key)
            )
        )
        .scalars()
        .all()
    )
    detailed = ctx.role.value in ("admin", "developer")  # raw evidence only for people who can act on it
    return {
        **_run_summary(r),
        "conditions": {k: v for k, v in r.conditions.items() if k not in ("fixtures", "lease_until")},
        "results": r.results,
        "cases": [
            {
                "case_key": x.case_key,
                "kind": x.kind,
                "category": x.category,
                "title": titles.get(x.case_key, x.case_key),
                "expected": expected.get(x.case_key),
                "verdict": x.verdict,
                "observed": x.observed if detailed else None,
                "evidence": x.evidence if detailed else None,
                "workflow_run_id": str(x.workflow_run_id) if (detailed and x.workflow_run_id) else None,
            }
            for x in results
        ],
    }


@router.get("/catalog")
async def catalog(session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    if ctx.role == Role.ADMIN or ctx.role == Role.DEVELOPER:
        await seed_catalog(session)
    cases = (
        (
            await session.execute(
                select(SecurityTestCase)
                .where(SecurityTestCase.suite_version == SUITE_VERSION)
                .order_by(SecurityTestCase.kind, SecurityTestCase.case_key)
            )
        )
        .scalars()
        .all()
    )
    total = (
        await session.execute(
            select(func.count())
            .select_from(SecurityTestCase)
            .where(SecurityTestCase.suite_version == SUITE_VERSION)
        )
    ).scalar_one()
    return {
        "suite_version": SUITE_VERSION,
        "total": total,
        "cases": [
            {
                "case_key": c.case_key,
                "kind": c.kind,
                "category": c.category,
                "title": c.title,
                "expected": c.expected,
                "sha256": c.content_sha256[:12],
            }
            for c in cases
        ],
    }


@router.get("/incidents")
async def incidents(
    session: SessionDep,
    ctx: WorkspaceDep,
    days: int = Query(30, ge=1, le=365),
    source: str | None = Query(None),
) -> dict[str, Any]:
    items = await list_incidents(session, ctx.workspace_id, days=days, limit=500)
    if source:
        items = [i for i in items if i["source"] == source]
    return {"items": items, "total": len(items)}
