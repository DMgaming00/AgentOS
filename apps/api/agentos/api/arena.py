"""Agent Arena API: corpus integrity, contenders, arena runs, leaderboards, the one-shot test split."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos_core.arena import leaderboard
from agentos_core.arena.corpus import FAMILIES, corpus_status
from agentos_core.arena.runner import (
    OFFLINE_PROVIDERS,
    ArenaError,
    arena_summary,
    create_arena,
    start_final_test,
)
from agentos_core.autopilot.evaluation import EvaluationError
from agentos_core.db.models import (
    AgentDefinition,
    ArenaEntry,
    ArenaRun,
    EvaluationCase,
    EvaluationCaseResult,
)
from agentos_core.domain.states import Role

router = APIRouter(prefix="/api/v1/arena", tags=["agent-arena"])
IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


class _Out(BaseModel):
    model_config = {"extra": "allow"}

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(**data)


class CreateArena(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    agent_version_ids: list[uuid.UUID] = Field(min_length=2, max_length=12)
    allow_live: bool = Field(
        default=False,
        description="admin only; permits real-provider contenders when paid requests are enabled",
    )


async def _arena(session: SessionDep, ctx: WorkspaceDep, arena_id: uuid.UUID) -> ArenaRun:
    a = await session.get(ArenaRun, arena_id)
    if a is None or a.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "arena run not found")
    return a


@router.get("/corpus")
async def corpus(session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    try:
        return await corpus_status(session, ctx.workspace_id)
    except EvaluationError as exc:
        raise api_error(exc.status, exc.code, str(exc)) from None


@router.get("/contenders")
async def contenders(session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    agents = (
        (
            await session.execute(
                select(AgentDefinition)
                .where(AgentDefinition.workspace_id == ctx.workspace_id)
                .order_by(AgentDefinition.name)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "agent_id": str(a.id),
                "name": a.name,
                "description": a.description,
                "provider": a.provider,
                "model": a.model,
                "tools": list(a.allowed_tool_ids),
                "max_steps": a.max_steps,
                "agent_version_id": str(a.active_version_id) if a.active_version_id else None,
                "eligible": a.provider in OFFLINE_PROVIDERS and a.active_version_id is not None,
                "live_eligible": a.provider not in OFFLINE_PROVIDERS and a.active_version_id is not None,
                "arena_contender": a.name.startswith("Arena ·"),
            }
            for a in agents
        ]
    }


@router.get("/runs")
async def runs(
    session: SessionDep, ctx: WorkspaceDep, limit: int = Query(20, ge=1, le=100)
) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(ArenaRun)
                .where(ArenaRun.workspace_id == ctx.workspace_id)
                .order_by(ArenaRun.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [await arena_summary(session, a) for a in rows]}


@router.post("/runs", status_code=201, dependencies=[rate_limited("critical")])
async def create(
    body: CreateArena, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.DEVELOPER)
    if body.allow_live:
        ctx.require(Role.ADMIN)  # live contenders spend money

    async def go() -> _Out:
        try:
            arena = await create_arena(
                session,
                workspace_id=ctx.workspace_id,
                user_id=ctx.user.id,
                name=body.name,
                agent_version_ids=body.agent_version_ids,
                allow_live=body.allow_live,
            )
        except EvaluationError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
        await session.flush()
        await session.refresh(arena)
        return _Out(await arena_summary(session, arena))

    return await idempotent(
        session, ctx, scope="arena:create", key=idempotency_key, body=body.model_dump(mode="json"), fn=go
    )


@router.get("/runs/{arena_id}")
async def detail(arena_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    arena = await _arena(session, ctx, arena_id)
    summary = await arena_summary(session, arena)
    out: dict[str, Any] = {**summary, "leaderboards": {}, "selection_vs_test": None}
    stage = summary["stage"]
    # Development and validation results are readable as soon as their evaluations finish; the test split is
    # published only when every test evaluation is complete, so a half-finished test leaderboard cannot guide choices.
    for split in ("development", "validation"):
        if stage in ("selection_complete", "final_test_running", "completed"):
            out["leaderboards"][split] = leaderboard.build_leaderboard(
                await leaderboard.load_scores(session, arena.id, split)
            )
    if stage == "completed":
        out["leaderboards"]["test"] = leaderboard.build_leaderboard(
            await leaderboard.load_scores(session, arena.id, "test")
        )
        out["selection_vs_test"] = leaderboard.selection_vs_test(
            out["leaderboards"]["validation"], out["leaderboards"]["test"]
        )
    return out


@router.post("/runs/{arena_id}/final-test", dependencies=[rate_limited("critical")])
async def final_test(
    arena_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.DEVELOPER)
    arena = await _arena(session, ctx, arena_id)

    async def go() -> _Out:
        try:
            a = await start_final_test(session, arena.id, user_id=ctx.user.id)
        except ArenaError as exc:
            raise api_error(exc.status, exc.code, str(exc)) from None
        await session.flush()
        await session.refresh(a)
        return _Out(await arena_summary(session, a))

    return await idempotent(
        session, ctx, scope=f"arena:final:{arena.id}", key=idempotency_key, body={}, fn=go
    )


@router.get("/runs/{arena_id}/cases")
async def cases(
    arena_id: uuid.UUID,
    session: SessionDep,
    ctx: WorkspaceDep,
    split: str = Query("validation", pattern="^(development|validation|test)$"),
    agent_version_id: uuid.UUID | None = None,
    family: str | None = Query(None),
    failed_only: bool = True,
    limit: int = Query(200, ge=1, le=500),
) -> dict[str, Any]:
    arena = await _arena(session, ctx, arena_id)
    summary = await arena_summary(session, arena)
    if split == "test" and summary["stage"] != "completed":
        raise api_error(
            409, "test_not_complete", "test-split cases are readable after the final test completes"
        )
    if family is not None and family not in FAMILIES:
        raise api_error(422, "invalid_family", f"family must be one of {', '.join(FAMILIES)}")
    stmt = (
        select(ArenaEntry, EvaluationCaseResult, EvaluationCase)
        .join(EvaluationCaseResult, EvaluationCaseResult.evaluation_run_id == ArenaEntry.evaluation_run_id)
        .join(EvaluationCase, EvaluationCase.id == EvaluationCaseResult.case_id)
        .where(ArenaEntry.arena_run_id == arena.id, ArenaEntry.split == split)
        .order_by(EvaluationCase.case_key)
        .limit(limit)
    )
    if agent_version_id:
        stmt = stmt.where(ArenaEntry.agent_version_id == agent_version_id)
    if family:
        stmt = stmt.where(ArenaEntry.family == family)
    if failed_only:
        stmt = stmt.where(EvaluationCaseResult.passed.is_not(True))
    rows = (await session.execute(stmt)).all()
    show_expected = split == "development"
    return {
        "items": [
            {
                "agent_version_id": str(e.agent_version_id),
                "family": e.family,
                "split": e.split,
                "case_key": c.case_key,
                "kind": (c.input or {}).get("kind"),
                "task": c.task if show_expected else None,
                "passed": r.passed,
                "category": (r.error or {}).get("category"),
                "error_type": (r.error or {}).get("type"),
                "expected": c.expected_output if show_expected else None,
                "observed": r.observed_output if show_expected else None,
                "workflow_run_id": str(r.workflow_run_id) if r.workflow_run_id else None,
            }
            for e, r, c in rows
        ],
        "expected_hidden": not show_expected,
    }
