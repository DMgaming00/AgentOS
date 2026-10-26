from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from agentos_core.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine(url: str | None = None, **kwargs: object) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        connect_args = dict(kwargs.pop("connect_args", None) or {})  # type: ignore[call-overload]
        # Name the connections after the process so operators (and the Chaos Lab) can tell whose they are.
        connect_args.setdefault("server_settings", {"application_name": f"agentos-{os.getpid()}"})
        _engine = create_async_engine(
            url or get_settings().database_url,
            pool_size=int(str(kwargs.pop("pool_size", 10))),
            max_overflow=int(str(kwargs.pop("max_overflow", 10))),
            pool_pre_ping=True,
            connect_args=connect_args,
            **kwargs,
        )
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine, _sessionmaker = None, None


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """One transaction: commit on success, roll back on any exception."""
    async with get_sessionmaker()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
