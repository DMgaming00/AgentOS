"""Idempotency-Key support for unsafe endpoints (run creation, approval decisions)."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import IdempotencyRecord
from agentos_core.util import canonical_json, sha256_hex


def request_hash(body: Any) -> str:
    return sha256_hex(canonical_json(body))


async def lookup(
    session: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID, scope: str, key: str, body: Any
) -> IdempotencyRecord | None:
    rec = (
        await session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.workspace_id == workspace_id,
                IdempotencyRecord.user_id == user_id,
                IdempotencyRecord.scope == scope,
                IdempotencyRecord.key == key,
            )
        )
    ).scalar_one_or_none()
    if rec is not None and rec.request_hash != request_hash(body):
        raise HTTPException(
            422,
            detail={
                "code": "idempotency_key_reused",
                "message": "Idempotency-Key was used with a different request body",
            },
        )
    return rec


async def store(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    scope: str,
    key: str,
    body: Any,
    status: int,
    response: dict[str, Any],
) -> None:
    await session.execute(
        insert(IdempotencyRecord)
        .values(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            user_id=user_id,
            scope=scope,
            key=key,
            request_hash=request_hash(body),
            response_status=status,
            response_body=response,
        )
        .on_conflict_do_nothing(constraint="uq_idempotency_scope_key")
    )


def validate_key(key: str | None) -> str | None:
    if key is None:
        return None
    if not (1 <= len(key) <= 200) or not key.isprintable():
        raise HTTPException(
            400,
            detail={
                "code": "invalid_idempotency_key",
                "message": "Idempotency-Key must be 1-200 printable characters",
            },
        )
    return key
