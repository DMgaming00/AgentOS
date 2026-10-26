"""Idempotency-Key handling for Autopilot state-changing endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi.responses import JSONResponse
from pydantic import BaseModel

from agentos.dependencies import SessionDep, WorkspaceDep
from agentos.security import idempotency


async def idempotent(
    session: SessionDep,
    ctx: WorkspaceDep,
    *,
    scope: str,
    key: str | None,
    body: Any,
    fn: Callable[[], Awaitable[BaseModel]],
    status: int = 201,
) -> Any:
    key = idempotency.validate_key(key)
    if key:
        rec = await idempotency.lookup(
            session, workspace_id=ctx.workspace_id, user_id=ctx.user.id, scope=scope, key=key, body=body
        )
        if rec is not None:
            return JSONResponse(
                rec.response_body, status_code=rec.response_status, headers={"Idempotent-Replayed": "true"}
            )
    out = await fn()
    payload = out.model_dump(mode="json")
    if key:
        await idempotency.store(
            session,
            workspace_id=ctx.workspace_id,
            user_id=ctx.user.id,
            scope=scope,
            key=key,
            body=body,
            status=status,
            response=payload,
        )
    return JSONResponse(payload, status_code=status)
