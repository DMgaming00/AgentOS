"""Start a deterministic re-execution of a recorded run.

Recorded replay (viewing) never creates anything. Re-execution creates a *new* run with ``purpose='replay'``:
the same workflow version and input, every agent step pinned to the exact agent version the original used,
model responses served from the original's recordings (``RecordedProvider``), only read-only tools allowed,
approval gates not requested. It is side-effect free by enforcement in the worker, and clearly distinct from the
recording it replays.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import ModelCallRecord, WorkflowDefinition, WorkflowRun, WorkflowStep
from agentos_core.domain.dag import WorkflowSpec
from agentos_core.domain.states import TERMINAL_RUN, RunStatus
from agentos_core.engine.core import EngineError, materialize_run
from agentos_core.workflow_service import validate_for_workspace


async def start_reexecution(
    session: AsyncSession, source_run_id: uuid.UUID, *, user_id: uuid.UUID | None
) -> WorkflowRun:
    src = await session.get(WorkflowRun, source_run_id)
    if src is None:
        raise EngineError("run not found", code="not_found")
    if src.status not in {s.value for s in TERMINAL_RUN} or src.status == RunStatus.CANCELLED:
        raise EngineError(
            "only finished (succeeded or failed) runs can be re-executed", code="run_not_replayable"
        )
    if src.purpose == "replay":
        raise EngineError("a re-execution cannot itself be re-executed", code="run_not_replayable")
    wf = await session.get(WorkflowDefinition, src.workflow_definition_id)
    assert wf is not None
    steps = list((await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == src.id))).scalars())
    has_agent_steps = any(s.node_type == "agent_task" for s in steps)
    recorded = (
        await session.execute(
            select(func.count())
            .select_from(ModelCallRecord)
            .where(ModelCallRecord.run_id == src.id, ModelCallRecord.status == "succeeded")
        )
    ).scalar_one()
    if has_agent_steps and recorded == 0:
        raise EngineError(
            "this run has no recorded model calls (it predates the Flight Recorder), so it cannot be re-executed",
            code="not_recorded",
        )
    result, agents = await validate_for_workspace(session, src.workspace_id, wf.definition)
    if not result.ok or result.spec is None:
        raise EngineError("the workflow no longer validates; cannot re-execute", code="invalid_workflow")
    spec: WorkflowSpec = result.spec
    overrides = {
        s.agent_definition_id: s.agent_version_id
        for s in steps
        if s.agent_definition_id and s.agent_version_id
    }
    return await materialize_run(
        session,
        workflow=wf,
        spec=spec,
        run_input=src.input,
        requested_by=user_id,
        agents_by_name=agents,
        version_overrides=overrides,
        purpose="replay",
        priority=src.priority,
        replay_of_run_id=src.id,
    )
