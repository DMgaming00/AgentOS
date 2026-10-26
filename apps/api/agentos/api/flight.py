"""Agent Flight Recorder: recorded playback, state at a frame, run comparison, deterministic re-execution, export.

Everything except ``reexecute`` is read-only over immutable records and never executes anything.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query
from fastapi.responses import Response
from sqlalchemy import select

from agentos.api._idem import idempotent
from agentos.api.runs import get_run_for, run_out
from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import RunOut
from agentos_core.db.models import EvaluationCaseResult, ModelCallRecord
from agentos_core.domain.states import Role
from agentos_core.engine.core import EngineError
from agentos_core.flight.diff import diff_recordings
from agentos_core.flight.export import ExportRefusedError, build_export, to_json
from agentos_core.flight.replay import start_reexecution
from agentos_core.flight.timeline import load_recording, state_at
from agentos_core.redaction import redact

router = APIRouter(prefix="/api/v1", tags=["flight-recorder"])


async def _bodies(session: Any, run_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    rows = (await session.execute(select(ModelCallRecord).where(ModelCallRecord.run_id == run_id))).scalars()
    return {
        str(r.id): {
            "request": redact(r.request),
            "response": redact(r.response) if r.response else None,
            "error": redact(r.error) if r.error else None,
            "truncated": r.truncated,
        }
        for r in rows
    }


@router.get("/runs/{run_id}/flight")
async def flight(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> dict[str, Any]:
    """The recorded execution: frames (events with status deltas), attempts, model calls, tools, approvals.

    Mode ``recorded``: reading this never executes a step, a tool or a model call.
    """
    await get_run_for(session, ctx, run_id)
    return await load_recording(session, run_id)


@router.get("/runs/{run_id}/flight/model-calls/{record_id}")
async def flight_model_call(
    run_id: uuid.UUID, record_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep
) -> dict[str, Any]:
    await get_run_for(session, ctx, run_id)
    rec = await session.get(ModelCallRecord, record_id)
    if rec is None or rec.run_id != run_id:
        raise api_error(404, "not_found", "model call record not found")
    return {
        "id": str(rec.id),
        "step_key": rec.node_key,
        "provider": rec.provider,
        "model": rec.model,
        "status": rec.status,
        "request_digest": rec.request_digest,
        "truncated": rec.truncated,
        "request": redact(rec.request),
        "response": redact(rec.response) if rec.response else None,
        "error": redact(rec.error) if rec.error else None,
    }


@router.get("/runs/{run_id}/flight/state")
async def flight_state(
    run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep, seq: int = Query(ge=0)
) -> dict[str, Any]:
    await get_run_for(session, ctx, run_id)
    rec = await load_recording(session, run_id)
    return {"seq": seq, "steps": state_at(rec, seq)}


@router.get("/flight/compare")
async def compare(
    session: SessionDep, ctx: WorkspaceDep, a: Annotated[uuid.UUID, Query()], b: Annotated[uuid.UUID, Query()]
) -> dict[str, Any]:
    """Structured side-by-side diff (steps, branches, retries, tool calls, model responses, outputs, cost)."""
    await get_run_for(session, ctx, a)
    await get_run_for(session, ctx, b)
    ra, rb = await load_recording(session, a), await load_recording(session, b)
    evaluation: dict[str, Any] | None = None
    results = {
        r.workflow_run_id: r
        for r in (
            await session.execute(
                select(EvaluationCaseResult).where(EvaluationCaseResult.workflow_run_id.in_([a, b]))
            )
        ).scalars()
    }
    if a in results and b in results:  # verdicts only: expected answers are never exposed here
        evaluation = {
            "passed": [results[a].passed, results[b].passed],
            "same_verdict": results[a].passed == results[b].passed,
        }
    return diff_recordings(ra, rb, await _bodies(session, a), await _bodies(session, b), evaluation)


@router.post(
    "/runs/{run_id}/reexecute",
    response_model=RunOut,
    status_code=201,
    dependencies=[rate_limited("critical")],
)
async def reexecute(
    run_id: uuid.UUID,
    session: SessionDep,
    ctx: WorkspaceDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> Any:
    """Deterministic re-execution: a NEW run (purpose=replay) fed from the recording. Side effects are refused."""
    ctx.require(Role.DEVELOPER)
    await get_run_for(session, ctx, run_id)

    async def go() -> RunOut:
        try:
            run = await start_reexecution(session, run_id, user_id=ctx.user.id)
        except EngineError as exc:
            raise api_error(exc.status_code, exc.code, str(exc)) from None
        await session.flush()
        await session.refresh(run)
        return await run_out(session, run)

    return await idempotent(
        session, ctx, scope=f"reexecute:{run_id}", key=idempotency_key, body={"run": str(run_id)}, fn=go
    )


@router.get("/runs/{run_id}/export")
async def export_run(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> Response:
    """Sanitized JSON package for bug reports. Fails closed if any credential-like content remains."""
    ctx.require(Role.DEVELOPER)
    await get_run_for(session, ctx, run_id)
    rec = await load_recording(session, run_id)
    try:
        package = build_export(rec, await _bodies(session, run_id))
    except ExportRefusedError as exc:
        raise api_error(409, "export_refused", str(exc)) from None
    return Response(
        content=to_json(package),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="agentos-flight-{str(run_id)[:8]}.json"'},
    )
