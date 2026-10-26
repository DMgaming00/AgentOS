"""Autopilot API: overview, failure intelligence, diagnoses, candidates, policy, events."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import RootModel, ValidationError
from sqlalchemy import func, select

from agentos.api._idem import idempotent
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import Page
from agentos.schemas.autopilot import (
    AnalyzeRequest,
    AutopilotEventOut,
    CandidateOut,
    DiagnoseRequest,
    DiagnosisDetail,
    DiagnosisOut,
    FailuresOut,
    GenerateCandidatesRequest,
    ManualCandidateRequest,
    SignalOut,
)
from agentos_core.autopilot.diagnosis import diagnose
from agentos_core.autopilot.events import autopilot_channel
from agentos_core.autopilot.failures import analyze, clusters
from agentos_core.autopilot.optimizer import (
    ProposedCandidate,
    ProposedChange,
    generate_candidates,
    propose_manual_candidate,
)
from agentos_core.autopilot.policy import AutopilotPolicy, load_policy
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    AgentDeployment,
    AutopilotEvent,
    DeploymentRequest,
    EvaluationRun,
    FailureDiagnosis,
    FailureSignal,
    ModelUsage,
    OptimizationCandidate,
    OptimizationExperiment,
    RolloutEvent,
    ToolExecution,
    Workspace,
)
from agentos_core.db.session import get_sessionmaker
from agentos_core.domain.errors import AgentOSError
from agentos_core.domain.states import Role
from agentos_core.notify import get_notifier
from agentos_core.util import utcnow

router = APIRouter(prefix="/api/v1/autopilot", tags=["autopilot"])


class CandidateList(RootModel[list[CandidateOut]]):
    pass


IdemKey = Annotated[str | None, Header(alias="Idempotency-Key")]


def _domain_error(exc: Exception) -> Exception:
    if isinstance(exc, AgentOSError):
        return api_error(
            409 if exc.error_type == "budget_exceeded" else (503 if exc.retryable else 422),
            exc.error_type,
            exc.message,
        )
    return api_error(422, "invalid_request", str(exc))


async def _agent(session: SessionDep, ctx: WorkspaceDep, agent_id: uuid.UUID) -> AgentDefinition:
    a = await session.get(AgentDefinition, agent_id)
    if a is None or a.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "agent not found")
    return a


async def candidate_out(session: SessionDep, c: OptimizationCandidate) -> CandidateOut:
    versions = dict(
        (
            await session.execute(
                select(AgentConfigVersion.id, AgentConfigVersion.version).where(
                    AgentConfigVersion.id.in_(
                        [v for v in (c.baseline_version_id, c.candidate_version_id) if v]
                    )
                )
            )
        ).all()
    )
    name = (
        await session.execute(select(AgentDefinition.name).where(AgentDefinition.id == c.agent_id))
    ).scalar_one()
    exp = (
        await session.execute(
            select(OptimizationExperiment.id)
            .where(OptimizationExperiment.candidate_id == c.id)
            .order_by(OptimizationExperiment.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return CandidateOut.model_validate(c).model_copy(
        update={
            "agent_name": name,
            "baseline_version": versions.get(c.baseline_version_id),
            "candidate_version": versions.get(c.candidate_version_id) if c.candidate_version_id else None,
            "latest_experiment_id": exp,
        }
    )


# ----------------------------------------------------------------------------- overview


@router.get("/overview")
async def overview(session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    """Counts from persisted Autopilot records (nothing estimated here)."""
    ws = ctx.workspace_id

    async def count(model: Any, *where: Any) -> int:
        return int(
            (await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        )

    agents = (
        (
            await session.execute(
                select(AgentDefinition)
                .where(AgentDefinition.workspace_id == ws)
                .order_by(AgentDefinition.name)
            )
        )
        .scalars()
        .all()
    )
    exp_rows = (
        (
            await session.execute(
                select(OptimizationExperiment).where(
                    OptimizationExperiment.workspace_id == ws, OptimizationExperiment.status == "completed"
                )
            )
        )
        .scalars()
        .all()
    )
    improvements = []
    for e in exp_rows:
        if e.decision == "eligible" and e.results:
            b = e.results["baseline"]["metrics"]["task_success_rate"]["value"]
            c = e.results["candidate"]["metrics"]["task_success_rate"]["value"]
            improvements.append(
                {
                    "experiment_id": str(e.id),
                    "baseline": b,
                    "candidate": c,
                    "demonstrated": bool((e.eligibility or {}).get("improvement_demonstrated")),
                }
            )
    regressions = await count(
        RolloutEvent,
        RolloutEvent.event_type == "regression_detected",
        RolloutEvent.deployment_id.in_(select(AgentDeployment.id).where(AgentDeployment.workspace_id == ws)),
    )
    per_agent = []
    for a in agents:
        v = await session.get(AgentConfigVersion, a.active_version_id) if a.active_version_id else None
        open_signals = await count(
            FailureSignal,
            FailureSignal.agent_id == a.id,
            FailureSignal.occurred_at >= utcnow() - timedelta(days=7),
        )
        latest = (
            await session.execute(
                select(OptimizationCandidate)
                .where(OptimizationCandidate.agent_id == a.id)
                .order_by(OptimizationCandidate.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        per_agent.append(
            {
                "agent_id": str(a.id),
                "name": a.name,
                "active_version": v.version if v else None,
                "canary_percent": a.canary_percent,
                "signals_7d": open_signals,
                "latest_candidate": {"id": str(latest.id), "title": latest.title, "status": latest.status}
                if latest
                else None,
            }
        )
    autopilot_spend = (
        await session.execute(
            select(func.coalesce(func.sum(ModelUsage.estimated_cost_usd), 0)).where(
                ModelUsage.workspace_id == ws, ModelUsage.purpose.in_(["diagnosis", "optimizer"])
            )
        )
    ).scalar_one()
    return {
        "agents_monitored": len(agents),
        "failures_detected": await count(FailureSignal, FailureSignal.workspace_id == ws),
        "agent_failures": await count(
            FailureSignal, FailureSignal.workspace_id == ws, FailureSignal.is_infrastructure.is_(False)
        ),
        "infrastructure_failures": await count(
            FailureSignal, FailureSignal.workspace_id == ws, FailureSignal.is_infrastructure.is_(True)
        ),
        "active_investigations": await count(
            FailureDiagnosis, FailureDiagnosis.workspace_id == ws, FailureDiagnosis.status == "hypothesis"
        ),
        "candidates": await count(OptimizationCandidate, OptimizationCandidate.workspace_id == ws),
        "candidates_rejected_by_policy": await count(
            OptimizationCandidate,
            OptimizationCandidate.workspace_id == ws,
            OptimizationCandidate.status == "rejected_policy",
        ),
        "experiments_completed": len(exp_rows),
        "experiments_not_eligible": sum(e.decision == "not_eligible" for e in exp_rows),
        "evaluations_running": await count(
            EvaluationRun, EvaluationRun.workspace_id == ws, EvaluationRun.status.in_(["queued", "running"])
        ),
        "approved_deployments": await count(
            AgentDeployment,
            AgentDeployment.workspace_id == ws,
            AgentDeployment.deployment_type.in_(["promotion", "canary"]),
        ),
        "rollbacks": await count(
            AgentDeployment, AgentDeployment.workspace_id == ws, AgentDeployment.deployment_type == "rollback"
        ),
        "pending_approvals": await count(
            DeploymentRequest, DeploymentRequest.workspace_id == ws, DeploymentRequest.status == "pending"
        ),
        "regressions_detected": regressions,
        "measured_improvements": improvements,
        "autopilot_spend_usd": float(autopilot_spend or 0),
        "agents": per_agent,
    }


# ----------------------------------------------------------------------------- failures


@router.get("/failures", response_model=FailuresOut)
async def failures(
    session: SessionDep,
    ctx: WorkspaceDep,
    window_hours: int = Query(168, ge=1, le=24 * 90),
    agent_id: uuid.UUID | None = None,
    category: str | None = None,
    limit: int = Query(100, ge=1, le=500),
) -> FailuresOut:
    since = utcnow() - timedelta(hours=window_hours)
    q = select(FailureSignal).where(
        FailureSignal.workspace_id == ctx.workspace_id, FailureSignal.occurred_at >= since
    )
    if agent_id:
        q = q.where(FailureSignal.agent_id == agent_id)
    if category:
        q = q.where(FailureSignal.category == category)
    signals = (
        (await session.execute(q.order_by(FailureSignal.occurred_at.desc()).limit(limit))).scalars().all()
    )
    groups = await clusters(session, ctx.workspace_id, since=since)
    names = dict(
        (
            await session.execute(
                select(AgentDefinition.id, AgentDefinition.name).where(
                    AgentDefinition.workspace_id == ctx.workspace_id
                )
            )
        ).all()
    )
    for g in groups:
        g["agent_name"] = names.get(uuid.UUID(g["agent_id"])) if g["agent_id"] else None
    diags = (
        (
            await session.execute(
                select(FailureDiagnosis)
                .where(FailureDiagnosis.workspace_id == ctx.workspace_id)
                .order_by(FailureDiagnosis.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    day = func.date_trunc("hour", FailureSignal.occurred_at)
    timeline = (
        await session.execute(
            select(day.label("h"), FailureSignal.category, func.count())
            .where(FailureSignal.workspace_id == ctx.workspace_id, FailureSignal.occurred_at >= since)
            .group_by("h", FailureSignal.category)
            .order_by("h")
        )
    ).all()
    tools = (
        await session.execute(
            select(ToolExecution.tool_name, ToolExecution.status, func.count())
            .join(FailureSignal, FailureSignal.run_id == ToolExecution.run_id)
            .where(
                FailureSignal.workspace_id == ctx.workspace_id,
                ToolExecution.status.in_(["failed", "denied"]),
                FailureSignal.occurred_at >= since,
            )
            .group_by(ToolExecution.tool_name, ToolExecution.status)
            .order_by(func.count().desc())
            .limit(10)
        )
    ).all()
    diagnosed = {(str(d.agent_id), d.failure_category) for d in diags}
    suggestions = [
        {
            "agent_id": g["agent_id"],
            "agent_name": g["agent_name"],
            "category": g["category"],
            "reason": f"{g['signals']} signals across {g['runs']} runs"
            + (
                " (infrastructure: check providers/workers before changing the agent)"
                if g["is_infrastructure"]
                else ""
            ),
            "action": "diagnose",
        }
        for g in groups
        if g["agent_id"] and (g["agent_id"], g["category"]) not in diagnosed and g["signals"] >= 2
    ][:8]
    return FailuresOut(
        window_hours=window_hours,
        clusters=groups,
        signals=[SignalOut.model_validate(s) for s in signals],
        diagnoses=[DiagnosisOut.model_validate(d) for d in diags],
        timeline=[{"hour": h.isoformat(), "category": c, "count": n} for h, c, n in timeline],
        top_tools=[{"tool": t, "status": st, "count": n} for t, st, n in tools],
        suggested_investigations=suggestions,
    )


@router.post("/failures/analyze", dependencies=[rate_limited("critical")])
async def analyze_failures(body: AnalyzeRequest, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    """Run the deterministic detectors over recent traces (idempotent: signals are fingerprinted)."""
    ctx.require(Role.DEVELOPER)
    if body.agent_id:
        await _agent(session, ctx, body.agent_id)
    result = await analyze(session, ctx.workspace_id, window_hours=body.window_hours, agent_id=body.agent_id)
    from agentos_core.autopilot.events import emit

    await emit(session, ctx.workspace_id, "failures.analyzed", result)
    return result


@router.get("/failures/{diagnosis_id}", response_model=DiagnosisDetail)
async def get_diagnosis(diagnosis_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> DiagnosisDetail:
    d = await session.get(FailureDiagnosis, diagnosis_id)
    if d is None or d.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "diagnosis not found")
    sigs = (
        (
            await session.execute(
                select(FailureSignal).where(FailureSignal.id.in_([uuid.UUID(x) for x in d.signal_ids]))
            )
        )
        .scalars()
        .all()
    )
    evidence = [
        {
            "signal_id": str(s.id),
            "summary": s.summary,
            "run_id": str(s.run_id),
            "category": s.category,
            "refs": s.evidence.get("refs", []),
            "cited": any(r["id"] in d.evidence_refs for r in s.evidence.get("refs", []))
            or f"signal:{s.id}" in d.evidence_refs,
        }
        for s in sigs
    ]
    return DiagnosisDetail(**DiagnosisOut.model_validate(d).model_dump(), evidence=evidence)


@router.post(
    "/diagnoses", response_model=DiagnosisOut, status_code=201, dependencies=[rate_limited("critical")]
)
async def create_diagnosis(
    body: DiagnoseRequest, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.DEVELOPER)
    await _agent(session, ctx, body.agent_id)

    async def run() -> DiagnosisOut:
        try:
            d = await diagnose(
                session,
                workspace_id=ctx.workspace_id,
                agent_id=body.agent_id,
                category=body.category,
                user_id=ctx.user.id,
                provider=body.provider,
            )
        except (AgentOSError, ValueError) as exc:
            raise _domain_error(exc) from None
        return DiagnosisOut.model_validate(d)

    return await idempotent(
        session, ctx, scope="diagnosis", key=idempotency_key, body=body.model_dump(mode="json"), fn=run
    )


# ----------------------------------------------------------------------------- candidates


@router.post(
    "/candidates/generate",
    response_model=list[CandidateOut],
    status_code=201,
    dependencies=[rate_limited("critical")],
)
async def generate(
    body: GenerateCandidatesRequest, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    ctx.require(Role.DEVELOPER)
    await _agent(session, ctx, body.agent_id)

    async def run() -> CandidateList:
        try:
            cands = await generate_candidates(
                session,
                workspace_id=ctx.workspace_id,
                agent_id=body.agent_id,
                diagnosis_id=body.diagnosis_id,
                user_id=ctx.user.id,
                count=body.count,
                provider=body.provider,
            )
        except (AgentOSError, ValueError) as exc:
            raise _domain_error(exc) from None
        return CandidateList([await candidate_out(session, c) for c in cands])

    return await idempotent(
        session, ctx, scope="candidates", key=idempotency_key, body=body.model_dump(mode="json"), fn=run
    )


@router.post("/candidates", response_model=CandidateOut, status_code=201)
async def manual_candidate(
    body: ManualCandidateRequest, session: SessionDep, ctx: WorkspaceDep, idempotency_key: IdemKey = None
) -> Any:
    """Human-authored candidate; validated by exactly the same policy as optimizer output."""
    ctx.require(Role.DEVELOPER)
    await _agent(session, ctx, body.agent_id)

    async def run() -> CandidateOut:
        try:
            c = await propose_manual_candidate(
                session,
                workspace_id=ctx.workspace_id,
                agent_id=body.agent_id,
                user_id=ctx.user.id,
                proposal=ProposedCandidate(
                    title=body.title,
                    changes=[ProposedChange(**ch.model_dump()) for ch in body.changes],
                    expected_benefits=body.expected_benefits,
                    risks=body.risks,
                ),
            )
        except (ValueError, ValidationError) as exc:
            raise _domain_error(exc) from None
        return await candidate_out(session, c)

    return await idempotent(
        session, ctx, scope="manual_candidate", key=idempotency_key, body=body.model_dump(mode="json"), fn=run
    )


@router.get("/candidates", response_model=Page[CandidateOut])
async def list_candidates(
    session: SessionDep,
    ctx: WorkspaceDep,
    agent_id: uuid.UUID | None = None,
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> Page[CandidateOut]:
    q = select(OptimizationCandidate).where(OptimizationCandidate.workspace_id == ctx.workspace_id)
    if agent_id:
        q = q.where(OptimizationCandidate.agent_id == agent_id)
    if status:
        q = q.where(OptimizationCandidate.status.in_(status.split(",")))
    total = (await session.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (
        await session.execute(q.order_by(OptimizationCandidate.created_at.desc()).limit(limit).offset(offset))
    ).scalars()
    return Page(
        items=[await candidate_out(session, c) for c in rows], total=total, limit=limit, offset=offset
    )


@router.get("/candidates/{candidate_id}", response_model=CandidateOut)
async def get_candidate(candidate_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> CandidateOut:
    c = await session.get(OptimizationCandidate, candidate_id)
    if c is None or c.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "candidate not found")
    return await candidate_out(session, c)


# ----------------------------------------------------------------------------- policy


@router.get("/policy", response_model=AutopilotPolicy)
async def get_policy(session: SessionDep, ctx: WorkspaceDep) -> AutopilotPolicy:
    ws = await session.get(Workspace, ctx.workspace_id)
    return load_policy(ws.settings if ws else {})


@router.put("/policy", response_model=AutopilotPolicy)
async def put_policy(body: AutopilotPolicy, session: SessionDep, ctx: WorkspaceDep) -> AutopilotPolicy:
    """Admins tune optimizer boundaries and gates. Never-optimizable fields are dropped on load."""
    ctx.require(Role.ADMIN)
    ws = await session.get(Workspace, ctx.workspace_id)
    assert ws is not None
    ws.settings = {**(ws.settings or {}), "autopilot": body.model_dump(mode="json")}
    await session.flush()
    return load_policy(ws.settings)


# ----------------------------------------------------------------------------- events


@router.get("/events", response_model=list[AutopilotEventOut])
async def list_events(
    session: SessionDep,
    ctx: WorkspaceDep,
    after: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
) -> list[AutopilotEventOut]:
    rows = (
        await session.execute(
            select(AutopilotEvent)
            .where(AutopilotEvent.workspace_id == ctx.workspace_id, AutopilotEvent.sequence_number > after)
            .order_by(AutopilotEvent.sequence_number)
            .limit(limit)
        )
    ).scalars()
    return [AutopilotEventOut.model_validate(e) for e in rows]


async def _autopilot_stream(ws_id: uuid.UUID, after: int, request: Request) -> AsyncIterator[str]:
    yield "retry: 2000\n\n"
    pubsub = None
    try:
        pubsub = get_notifier().client.pubsub()
        await asyncio.wait_for(pubsub.subscribe(autopilot_channel(ws_id)), timeout=2)
    except Exception:
        pubsub = None
    last, idle = after, 0.0
    try:
        while not await request.is_disconnected():
            async with get_sessionmaker()() as s:
                rows = (
                    (
                        await s.execute(
                            select(AutopilotEvent)
                            .where(
                                AutopilotEvent.workspace_id == ws_id, AutopilotEvent.sequence_number > last
                            )
                            .order_by(AutopilotEvent.sequence_number)
                            .limit(200)
                        )
                    )
                    .scalars()
                    .all()
                )
            for e in rows:
                yield (
                    f"id: {e.sequence_number}\nevent: {e.event_type}\n"
                    f"data: {AutopilotEventOut.model_validate(e).model_dump_json()}\n\n"
                )
                last = e.sequence_number
            if rows:
                idle = 0.0
                continue
            start = asyncio.get_running_loop().time()
            if pubsub is not None:
                with contextlib.suppress(Exception):
                    await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            else:
                await asyncio.sleep(1.0)
            idle += asyncio.get_running_loop().time() - start
            if idle >= 15:
                idle = 0.0
                yield ": keepalive\n\n"
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]


@router.get(
    "/stream", response_class=StreamingResponse, responses={200: {"content": {"text/event-stream": {}}}}
)
async def stream(
    request: Request,
    ctx: WorkspaceDep,
    after: int = Query(0, ge=0),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    start = max(after, int(last_event_id)) if last_event_id and last_event_id.isdigit() else after
    return StreamingResponse(
        _autopilot_stream(ctx.workspace_id, start, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
