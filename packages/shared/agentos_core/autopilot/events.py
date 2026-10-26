"""Workspace-scoped Autopilot event log (diagnosis, candidates, evaluations, deployments)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import AutopilotEvent, Workspace
from agentos_core.engine.tx import queue_notify
from agentos_core.redaction import redact

AUTOPILOT_EVENT_TYPES = frozenset(
    {
        "failures.analyzed",
        "diagnosis.started",
        "diagnosis.completed",
        "diagnosis.rejected",
        "candidate.generated",
        "candidate.rejected",
        "evaluation.started",
        "evaluation.completed",
        "experiment.started",
        "experiment.completed",
        "approval.requested",
        "approval.decided",
        "version.promoted",
        "version.rolled_back",
        "canary.started",
        "monitoring.regression_detected",
        "dataset.imported",
    }
)


def autopilot_channel(workspace_id: object) -> str:
    return f"agentos:autopilot:{workspace_id}"


async def emit(
    session: AsyncSession, workspace_id: uuid.UUID, event_type: str, payload: dict[str, Any]
) -> None:
    """Append an event with a gap-free per-workspace sequence (locks the workspace row briefly).

    Never call this while holding a run row lock: Autopilot transactions take the workspace
    lock last and do not lock runs, which keeps lock ordering acyclic.
    """
    if event_type not in AUTOPILOT_EVENT_TYPES:
        raise ValueError(f"unknown autopilot event {event_type!r}")
    seq = (
        await session.execute(
            update(Workspace)
            .where(Workspace.id == workspace_id)
            .values(autopilot_seq=Workspace.autopilot_seq + 1)
            .returning(Workspace.autopilot_seq)
        )
    ).scalar_one()
    session.add(
        AutopilotEvent(
            workspace_id=workspace_id, sequence_number=seq, event_type=event_type, payload=redact(payload)
        )
    )
    queue_notify(session, autopilot_channel(workspace_id), {"seq": seq})
