"""Worker fleet health from heartbeats and recent attempts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from agentos.dependencies import SessionDep, WorkspaceDep
from agentos_core.observability import workers_view

router = APIRouter(prefix="/api/v1/workers", tags=["workers"])


@router.get("")
async def list_workers(
    session: SessionDep, ctx: WorkspaceDep, window_minutes: int = Query(60, ge=5, le=1440)
) -> dict[str, Any]:
    return await workers_view(session, window_minutes=window_minutes)
