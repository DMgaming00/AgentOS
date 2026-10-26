"""Transaction helpers: post-commit notifications and deadlock retry.

Lock ordering rule (prevents deadlocks): any transaction that mutates a run's
steps, events, approvals or counters locks the ``workflow_runs`` row *first*
(``lock_run``), then step rows. Single-row lease renewals are the only exception
and they hold no other locks.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import WorkflowRun
from agentos_core.db.session import get_sessionmaker
from agentos_core.log import get_logger
from agentos_core.notify import WORK_CHANNEL, get_notifier, run_channel

log = get_logger(__name__)
T = TypeVar("T")

RETRYABLE_SQLSTATES = {"40P01", "40001"}  # deadlock_detected, serialization_failure


def queue_notify(session: AsyncSession, channel: str, message: dict[str, Any]) -> None:
    session.info.setdefault("notify", []).append((channel, message))


def notify_run(session: AsyncSession, run_id: uuid.UUID, seq: int) -> None:
    queue_notify(session, run_channel(run_id), {"run_id": str(run_id), "seq": seq})


def notify_work(session: AsyncSession) -> None:
    queue_notify(session, WORK_CHANNEL, {"kind": "work"})


async def flush_notifications(session: AsyncSession) -> None:
    pending: list[tuple[str, dict[str, Any]]] = session.info.pop("notify", [])
    seen: set[str] = set()
    notifier = get_notifier()
    for channel, message in pending:
        key = f"{channel}:{message}"
        if key in seen:
            continue
        seen.add(key)
        await notifier.publish(channel, message)


@asynccontextmanager
async def unit_of_work() -> AsyncIterator[AsyncSession]:
    """A single transaction. Notifications queued inside are published only after commit."""
    from agentos_core.engine.events import flush_events

    async with get_sessionmaker()() as session:
        try:
            yield session
            await flush_events(session)
            await session.commit()
        except BaseException:
            await session.rollback()
            session.info.pop("notify", None)
            session.info.pop("pending_events", None)
            raise
        await flush_notifications(session)


def _is_retryable_db_error(exc: BaseException) -> bool:
    if isinstance(exc, DBAPIError):
        orig = getattr(exc, "orig", None)
        code = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
        return code in RETRYABLE_SQLSTATES
    return False


async def run_in_transaction[T](fn: Callable[[AsyncSession], Awaitable[T]], *, attempts: int = 4) -> T:
    """Run ``fn`` in a fresh transaction, retrying on deadlock/serialization failure."""
    for i in range(attempts):
        try:
            async with unit_of_work() as session:
                return await fn(session)
        except DBAPIError as exc:
            if not _is_retryable_db_error(exc) or i == attempts - 1:
                raise
            log.warning("transaction_retry", attempt=i + 1, error=str(exc.orig))
            await asyncio.sleep(0.02 * (2**i))
    raise AssertionError("unreachable")


async def lock_run(
    session: AsyncSession, run_id: uuid.UUID, *, skip_locked: bool = False
) -> WorkflowRun | None:
    stmt = (
        select(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
