"""Agent Chaos Lab API: fault catalog, scenarios, seeded experiments, resilience scorecards, before/after."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos_core.chaos.compare import compare_experiments
from agentos_core.chaos.experiments import ChaosError, abort_experiment, create_experiment
from agentos_core.chaos.faults import CATALOG, MAX_DURATION_SECONDS, MAX_RUNS, PRESETS, ChaosPlan
from agentos_core.chaos.scorecard import METRIC_DEFINITIONS
from agentos_core.db.models import (
    ChaosExperiment,
    ChaosInjection,
    FaultScenario,
    OptimizationCandidate,
)
from agentos_core.domain.states import Role

router = APIRouter(prefix="/api/v1/chaos", tags=["chaos-lab"])


class ScenarioCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=100)
    description: str = Field(default="", max_length=1000)
    plan: ChaosPlan


class ExperimentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=200)
    workflow_id: uuid.UUID
    scenario: str | None = Field(default=None, description="name of a built-in or saved scenario")
    plan: ChaosPlan | None = None
    overrides: dict[str, Any] = Field(
        default_factory=dict, description="fields of the scenario's plan to change"
    )
    candidate_id: uuid.UUID | None = Field(
        default=None, description="Autopilot candidate whose agent version is tested"
    )
    version_overrides: dict[uuid.UUID, uuid.UUID] = Field(
        default_factory=dict, description="agent id -> version id"
    )


class CompareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_id: uuid.UUID
    candidate_id: uuid.UUID


def _summary(e: ChaosExperiment) -> dict[str, Any]:
    r = e.results or {}
    m = r.get("metrics", {})
    return {
        "id": str(e.id),
        "name": e.name,
        "status": e.status,
        "workflow_definition_id": str(e.workflow_definition_id),
        "created_at": e.created_at.isoformat(),
        "started_at": e.started_at.isoformat() if e.started_at else None,
        "completed_at": e.completed_at.isoformat() if e.completed_at else None,
        "seed": e.plan["seed"],
        "trials": e.plan["trials"],
        "plan_sha256": e.plan_sha256,
        "faults": [f["type"] for f in e.plan["faults"]],
        "conditions": e.conditions,
        "abort_reason": e.abort_reason,
        "completion_rate": (m.get("task_completion_rate") or {}).get("value"),
        "injections_total": r.get("injections_total"),
        "overrides": e.version_overrides,
    }


async def _get(session: Any, ctx: Any, experiment_id: uuid.UUID) -> ChaosExperiment:
    e: ChaosExperiment | None = await session.get(ChaosExperiment, experiment_id)
    if e is None or e.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "experiment not found")
    return e


@router.get("/catalog")
async def catalog(_: WorkspaceDep) -> dict[str, Any]:
    return {
        "faults": [
            {
                "type": t.value,
                "points": [p.value for p in m["points"]],
                "default_point": m["default"].value,
                "description": m["what"],
                "expected_platform_response": m["expect"],
            }
            for t, m in CATALOG.items()
        ],
        "limits": {
            "max_runs": MAX_RUNS,
            "max_duration_seconds": MAX_DURATION_SECONDS,
            "max_safety_budget_usd": 25,
        },
        "metrics": METRIC_DEFINITIONS,
        "notes": [
            "Faults are injected at real boundaries (provider call, tool execution, worker process, Redis, PostgreSQL, run budget).",
            "Approvals in experiments are decided by a simulated reviewer.",
            "No combined resilience score is computed.",
        ],
    }


@router.get("/scenarios")
async def scenarios(session: SessionDep, ctx: WorkspaceDep) -> list[dict[str, Any]]:
    custom = (
        (
            await session.execute(
                select(FaultScenario)
                .where(FaultScenario.workspace_id == ctx.workspace_id)
                .order_by(FaultScenario.name)
            )
        )
        .scalars()
        .all()
    )
    out = [
        {
            "id": None,
            "name": n,
            "description": p["description"],
            "builtin": True,
            "plan": ChaosPlan.model_validate(p["plan"]).model_dump(mode="json"),
        }
        for n, p in PRESETS.items()
    ]
    out += [
        {"id": str(s.id), "name": s.name, "description": s.description, "builtin": False, "plan": s.plan}
        for s in custom
        if s.name not in PRESETS
    ]
    return out


@router.post("/scenarios", status_code=201, dependencies=[rate_limited("critical")])
async def create_scenario(body: ScenarioCreate, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    ctx.require(Role.DEVELOPER)
    if body.name in PRESETS:
        raise api_error(409, "name_reserved", "that name belongs to a built-in scenario")
    exists = (
        await session.execute(
            select(FaultScenario.id).where(
                FaultScenario.workspace_id == ctx.workspace_id, FaultScenario.name == body.name
            )
        )
    ).scalar_one_or_none()
    if exists:
        raise api_error(409, "exists", "a scenario with that name already exists")
    sc = FaultScenario(
        workspace_id=ctx.workspace_id,
        name=body.name,
        description=body.description,
        plan=body.plan.model_dump(mode="json"),
        created_by=ctx.user.id,
    )
    session.add(sc)
    await session.flush()
    return {
        "id": str(sc.id),
        "name": sc.name,
        "description": sc.description,
        "builtin": False,
        "plan": sc.plan,
    }


@router.post("/experiments", status_code=201, dependencies=[rate_limited("critical")])
async def start_experiment(
    body: ExperimentCreate,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """Create the experiment and its seeded runs. Faults fire only for these runs, only while the window is open."""
    ctx.require(Role.DEVELOPER)
    if (body.scenario is None) == (body.plan is None):
        raise api_error(422, "invalid_request", "provide exactly one of 'scenario' or 'plan'")
    plan = body.plan
    if body.scenario:
        raw: dict[str, Any] | None = PRESETS[body.scenario]["plan"] if body.scenario in PRESETS else None
        if raw is None:
            sc = (
                await session.execute(
                    select(FaultScenario).where(
                        FaultScenario.workspace_id == ctx.workspace_id, FaultScenario.name == body.scenario
                    )
                )
            ).scalar_one_or_none()
            if sc is None:
                raise api_error(404, "not_found", "scenario not found")
            raw = sc.plan
        try:
            plan = ChaosPlan.model_validate({**raw, **body.overrides})
        except ValueError as exc:
            raise api_error(422, "invalid_plan", str(exc)) from None
    assert plan is not None
    overrides = dict(body.version_overrides)
    if body.candidate_id:
        cand = await session.get(OptimizationCandidate, body.candidate_id)
        if cand is None or cand.workspace_id != ctx.workspace_id or cand.candidate_version_id is None:
            raise api_error(404, "not_found", "candidate not found or has no version")
        overrides[cand.agent_id] = cand.candidate_version_id

    async def go() -> Any:
        try:
            exp = await create_experiment(
                session,
                workspace_id=ctx.workspace_id,
                user_id=ctx.user.id,
                name=body.name,
                workflow_id=body.workflow_id,
                plan=plan,
                version_overrides=overrides,
            )
        except ChaosError as exc:
            raise api_error(exc.status_code, exc.code, str(exc)) from None
        await session.flush()
        await session.refresh(exp)
        return _Out(_summary(exp))

    return await idempotent(
        session, ctx, scope="chaos:start", key=idempotency_key, body=body.model_dump(mode="json"), fn=go
    )


class _Out(BaseModel):
    """Passes an already-built dict through ``idempotent`` (which stores and replays model dumps)."""

    model_config = ConfigDict(extra="allow")

    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(**data)


@router.get("/experiments")
async def list_experiments(
    session: SessionDep, ctx: WorkspaceDep, limit: int = Query(50, ge=1, le=200)
) -> dict[str, Any]:
    rows = (
        (
            await session.execute(
                select(ChaosExperiment)
                .where(ChaosExperiment.workspace_id == ctx.workspace_id)
                .order_by(ChaosExperiment.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return {"items": [_summary(e) for e in rows], "total": len(rows)}


@router.get("/experiments/{experiment_id}")
async def get_experiment(experiment_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    e = await _get(session, ctx, experiment_id)
    inj = (
        (
            await session.execute(
                select(ChaosInjection)
                .where(ChaosInjection.experiment_id == e.id)
                .order_by(ChaosInjection.injected_at)
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    return {
        **_summary(e),
        "plan": e.plan,
        "results": e.results,
        "version_overrides": e.version_overrides,
        "injections": [
            {
                "id": str(i.id),
                "run_id": str(i.run_id) if i.run_id else None,
                "case_index": i.case_index,
                "fault_type": i.fault_type,
                "point": i.point,
                "step_key": i.step_key,
                "attempt": i.attempt_number,
                "call_key": i.call_key,
                "detail": i.detail,
                "outcome": i.outcome,
                "at": i.injected_at.isoformat(),
            }
            for i in inj
        ],
    }


@router.post("/experiments/{experiment_id}/abort", dependencies=[rate_limited("critical")])
async def abort(experiment_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    ctx.require(Role.DEVELOPER)
    await _get(session, ctx, experiment_id)
    try:
        e = await abort_experiment(session, experiment_id, f"aborted by {ctx.user.email}")
    except ChaosError as exc:
        raise api_error(exc.status_code, exc.code, str(exc)) from None
    return _summary(e)


@router.post("/compare")
async def compare(body: CompareRequest, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    a = await _get(session, ctx, body.baseline_id)
    b = await _get(session, ctx, body.candidate_id)
    try:
        return compare_experiments(a, b)
    except ChaosError as exc:
        raise api_error(exc.status_code, exc.code, str(exc)) from None
