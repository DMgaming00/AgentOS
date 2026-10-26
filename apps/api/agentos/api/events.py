"""Run events: paginated history and a Server-Sent Events stream.

The stream replays persisted events after ``Last-Event-ID`` (or ``?after=``),
then waits for Redis notifications - or polls every second if Redis is down -
and always re-reads from PostgreSQL, so a missed notification never loses an event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from agentos.api.runs import get_run_for
from agentos.dependencies import SessionDep, WorkspaceDep
from agentos.schemas.api import EventOut
from agentos_core.db.models import Event, WorkflowRun
from agentos_core.db.session import get_sessionmaker
from agentos_core.domain.states import TERMINAL_RUN
from agentos_core.notify import get_notifier, run_channel

router = APIRouter(prefix="/api/v1/runs", tags=["events"])
KEEPALIVE_SECONDS = 15.0


@router.get("/{run_id}/events", response_model=list[EventOut])
async def list_events(
    run_id: uuid.UUID,
    session: SessionDep,
    ctx: WorkspaceDep,
    after: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=2000),
) -> list[EventOut]:
    await get_run_for(session, ctx, run_id)
    rows = (
        await session.execute(
            select(Event)
            .where(Event.run_id == run_id, Event.sequence_number > after)
            .order_by(Event.sequence_number)
            .limit(limit)
        )
    ).scalars()
    return [EventOut.model_validate(e) for e in rows]


def format_sse(event: Event) -> str:
    data = EventOut.model_validate(event).model_dump_json()
    return f"id: {event.sequence_number}\nevent: {event.event_type}\ndata: {data}\n\n"


async def _fetch(run_id: uuid.UUID, after: int) -> tuple[list[Event], str]:
    async with get_sessionmaker()() as session:
        events = list(
            (
                await session.execute(
                    select(Event)
                    .where(Event.run_id == run_id, Event.sequence_number > after)
                    .order_by(Event.sequence_number)
                    .limit(500)
                )
            ).scalars()
        )
        status = (
            await session.execute(select(WorkflowRun.status).where(WorkflowRun.id == run_id))
        ).scalar_one()
    return events, status


async def event_stream(run_id: uuid.UUID, after: int, request: Request) -> AsyncIterator[str]:
    yield "retry: 2000\n\n"
    pubsub = None
    try:
        pubsub = get_notifier().client.pubsub()
        await asyncio.wait_for(pubsub.subscribe(run_channel(run_id)), timeout=2)
    except Exception:
        pubsub = None  # degrade to polling
    last = after
    idle = 0.0
    try:
        while not await request.is_disconnected():
            events, status = await _fetch(run_id, last)
            for e in events:
                yield format_sse(e)
                last = e.sequence_number
            if not events and status in TERMINAL_RUN:
                yield f"event: stream.end\ndata: {json.dumps({'status': status, 'last_sequence': last})}\n\n"
                return
            if events:
                idle = 0.0
                continue
            waited = await _wait(pubsub, 1.0)
            idle += waited
            if idle >= KEEPALIVE_SECONDS:
                idle = 0.0
                yield ": keepalive\n\n"
    finally:
        if pubsub is not None:
            with contextlib.suppress(Exception):
                await pubsub.aclose()  # type: ignore[no-untyped-call]


async def _wait(pubsub, timeout: float) -> float:  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    start = loop.time()
    if pubsub is None:
        await asyncio.sleep(timeout)
    else:
        try:
            await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
        except Exception:
            await asyncio.sleep(timeout)
    return loop.time() - start


@router.get(
    "/{run_id}/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream(
    run_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    ctx: WorkspaceDep,
    after: int = Query(0, ge=0),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    await get_run_for(session, ctx, run_id)
    start = after
    if last_event_id and last_event_id.isdigit():
        start = max(start, int(last_event_id))
    return StreamingResponse(
        event_stream(run_id, start, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
