"""Persistent, per-run ordered event log.

Events are inserted in the same transaction as the state change they describe,
then published to Redis after commit. The SSE endpoint replays from PostgreSQL,
so a missed notification never loses state.
"""

from __future__ import annotations

import uuid
from typing import Any

from opentelemetry import trace
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.config import get_settings
from agentos_core.db.models import Event, WorkflowRun
from agentos_core.engine.tx import notify_run
from agentos_core.redaction import redact
from agentos_core.util import utcnow

EVENT_TYPES = frozenset(
    {
        "run.created",
        "run.started",
        "run.planned",
        "step.ready",
        "step.started",
        "model.call.started",
        "model.call.completed",
        "model.call.failed",
        "tool.started",
        "tool.completed",
        "tool.failed",
        "tool.denied",
        "approval.requested",
        "approval.resolved",
        "step.retrying",
        "step.failed",
        "step.completed",
        "step.skipped",
        "step.cancelled",
        "step.suspended",
        "step.stale_result_rejected",
        "run.waiting_approval",
        "run.resumed",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "budget.exceeded",
    }
)


PENDING_KEY = "pending_events"


def current_trace_id() -> str | None:
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


async def append_event(
    session: AsyncSession,
    run_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    step_id: uuid.UUID | None = None,
) -> Event:
    """Buffer an event; sequence numbers are assigned in one batch by ``flush_events``.

    The caller must hold the run row lock (see ``lock_run``) for the rest of the
    transaction, which is what makes per-run sequence numbers gap-free and ordered.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {event_type!r}")
    event = Event(
        run_id=run_id,
        step_id=step_id,
        event_type=event_type,
        timestamp=utcnow(),
        payload=redact(payload or {}),
        trace_id=current_trace_id(),
    )
    if not get_settings().event_batching:
        # Unbatched mode (kept for A/B benchmarking): sequence number assigned immediately.
        event.sequence_number = (
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(event_seq=WorkflowRun.event_seq + 1)
                .returning(WorkflowRun.event_seq)
            )
        ).scalar_one()
        session.add(event)
        notify_run(session, run_id, event.sequence_number)
        return event
    session.info.setdefault(PENDING_KEY, []).append(event)
    return event


async def flush_events(session: AsyncSession) -> None:
    """Assign sequence numbers (one UPDATE per run) and insert all buffered events (one batch).

    Called right before commit by ``unit_of_work`` and the API session dependency.
    Replaces an UPDATE + INSERT round trip per event inside the run-locked section.
    """
    pending: list[Event] = session.info.pop(PENDING_KEY, [])
    if not pending:
        return
    by_run: dict[uuid.UUID, list[Event]] = {}
    for e in pending:
        by_run.setdefault(e.run_id, []).append(e)
    for run_id, events in by_run.items():
        end = (
            await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(event_seq=WorkflowRun.event_seq + len(events))
                .returning(WorkflowRun.event_seq)
            )
        ).scalar_one()
        for i, e in enumerate(events):
            e.sequence_number = end - len(events) + i + 1
        notify_run(session, run_id, end)
    session.add_all(pending)
    await session.flush()
