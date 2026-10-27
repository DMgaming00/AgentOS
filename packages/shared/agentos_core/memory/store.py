"""PostgreSQL-backed memory store: create, read, update, delete, expire, version, audit, retrieve.

Rules enforced here (not left to callers):

* every query is scoped by ``workspace_id``; a record id from another workspace is "not found";
* content that looks like a credential is refused (``sensitive_content``) and the refusal is audited without the content;
  nothing is stored automatically: writes are explicit calls (agents need the approval-gated ``memory_write`` tool);
* updates are optimistic (``expected_version``) and every state is kept in ``memory_versions`` (append-only);
* expired records disappear from reads and retrieval at the instant ``expires_at`` passes; ``expire_due`` then records the
  expiry in the version history and the audit log;
* retrieval returns provenance and a relevance score and marks every result ``untrusted``: memory text is context, never
  instructions.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import MemoryAuditEvent, MemoryRecord, MemoryVersion
from agentos_core.redaction import SECRET_PATTERNS, SENSITIVE_KEYS, strip_nul
from agentos_core.tools.document_search import build_tsquery
from agentos_core.util import sha256_hex, utcnow

MAX_CONTENT_CHARS = 8000
MAX_TAGS = 20
KINDS = ("kv", "note", "task_summary")
_KV_SECRET = re.compile(r"(?i)\b(" + SENSITIVE_KEYS.pattern.strip("()") + r")\b\s*[:=]\s*\S{8,}")


class MemoryStoreError(Exception):
    def __init__(self, message: str, code: str = "memory_error", status: int = 409) -> None:
        super().__init__(message)
        self.code, self.status = code, status


@dataclass(frozen=True)
class Actor:
    type: str  # user | agent | system
    id: str | None = None


SYSTEM = Actor("system")


def looks_sensitive(content: str) -> bool:
    """Credential-shaped strings and ``password: hunter2hunter2``-style pairs. A heuristic, not a data-loss-prevention system."""
    return any(p.search(content) for p in SECRET_PATTERNS) or bool(_KV_SECRET.search(content))


async def _audit(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    action: str,
    actor: Actor,
    record_id: uuid.UUID | None = None,
    **details: Any,
) -> None:
    session.add(
        MemoryAuditEvent(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            record_id=record_id,
            action=action,
            actor_type=actor.type,
            actor_id=actor.id,
            details=strip_nul(details),
        )
    )


async def _audit_denied(workspace_id: uuid.UUID, actor: Actor, **details: Any) -> None:
    """Audit a refusal in its own transaction: the caller is about to fail, and a rolled-back refusal would leave no trace.
    (It references no record row, so it cannot wait on locks the caller holds.)"""
    from agentos_core.engine.tx import unit_of_work

    async with unit_of_work() as session:
        await _audit(session, workspace_id, "denied", actor, None, **details)


def _live(now: datetime) -> Any:
    return (
        MemoryRecord.deleted_at.is_(None),
        or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > now),
    )


def _validate(content: str, tags: list[str], kind: str, key: str | None) -> tuple[str, list[str]]:
    content = strip_nul(content).strip()
    if kind not in KINDS:
        raise MemoryStoreError(f"kind must be one of {', '.join(KINDS)}", "invalid_kind", 422)
    if not content:
        raise MemoryStoreError("content is empty", "empty_content", 422)
    if len(content) > MAX_CONTENT_CHARS:
        raise MemoryStoreError(f"content exceeds {MAX_CONTENT_CHARS} characters", "too_large", 422)
    if kind == "kv" and not key:
        raise MemoryStoreError("kv records need a key", "key_required", 422)
    clean = sorted({str(t)[:50] for t in tags})
    if len(clean) > MAX_TAGS:
        raise MemoryStoreError(f"at most {MAX_TAGS} tags", "too_many_tags", 422)
    return content, clean


async def create_record(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    kind: str,
    content: str,
    actor: Actor,
    namespace: str = "default",
    key: str | None = None,
    tags: list[str] | None = None,
    source: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    created_by: uuid.UUID | None = None,
) -> MemoryRecord:
    content, clean = _validate(content, tags or [], kind, key)
    if looks_sensitive(content):
        await _audit_denied(
            workspace_id, actor, reason="sensitive_content", operation="create", namespace=namespace, key=key
        )
        raise MemoryStoreError(
            "content looks like a credential or secret and was not stored", "sensitive_content", 422
        )
    if expires_at is not None and expires_at <= utcnow():
        raise MemoryStoreError("expires_at must be in the future", "invalid_expiry", 422)
    if key is not None:
        live = (
            await session.execute(
                select(MemoryRecord.id).where(
                    MemoryRecord.workspace_id == workspace_id,
                    MemoryRecord.namespace == namespace,
                    MemoryRecord.key == key,
                    MemoryRecord.deleted_at.is_(None),
                )
            )
        ).first()
        if live is not None:
            raise MemoryStoreError(
                f"key {key!r} already exists in namespace {namespace!r}; update it instead", "key_exists"
            )
    now = utcnow()
    digest = sha256_hex(content)
    rec = MemoryRecord(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        namespace=namespace,
        kind=kind,
        key=key,
        content=content,
        tags=clean,
        source={**(source or {}), "actor": actor.type, "actor_id": actor.id},
        version=1,
        content_sha256=digest,
        expires_at=expires_at,
        created_by=created_by,
        updated_by=created_by,
        created_at=now,
        updated_at=now,
    )
    session.add(rec)
    await session.flush()
    session.add(_version(rec, "create", actor))
    await _audit(session, workspace_id, "create", actor, rec.id, namespace=namespace, key=key, version=1)
    await session.flush()
    return rec


def _version(rec: MemoryRecord, change: str, actor: Actor) -> MemoryVersion:
    return MemoryVersion(
        id=uuid.uuid4(),
        record_id=rec.id,
        version=rec.version,
        change_type=change,
        content=rec.content,
        tags=list(rec.tags),
        content_sha256=rec.content_sha256,
        changed_by=f"{actor.type}:{actor.id}" if actor.id else actor.type,
    )


async def get_record(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    record_id: uuid.UUID,
    *,
    actor: Actor,
    audit: bool = True,
    include_inactive: bool = False,
    lock: bool = False,
) -> MemoryRecord:
    q = select(MemoryRecord).where(MemoryRecord.id == record_id, MemoryRecord.workspace_id == workspace_id)
    if not include_inactive:
        q = q.where(*_live(utcnow()))
    if lock:
        q = q.with_for_update()
    rec = (await session.execute(q)).scalar_one_or_none()
    if rec is None:
        raise MemoryStoreError("memory record not found", "not_found", 404)
    if audit:
        await _audit(session, workspace_id, "read", actor, rec.id, version=rec.version)
    return rec


_UNSET: Any = object()


async def update_record(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    record_id: uuid.UUID,
    *,
    expected_version: int,
    actor: Actor,
    content: str | None = None,
    tags: list[str] | None = None,
    expires_at: Any = _UNSET,
    updated_by: uuid.UUID | None = None,
) -> MemoryRecord:
    rec = await get_record(session, workspace_id, record_id, actor=actor, audit=False, lock=True)
    if rec.version != expected_version:
        raise MemoryStoreError(
            f"record is at version {rec.version}, not {expected_version}", "version_conflict"
        )
    new_content, new_tags = _validate(
        content if content is not None else rec.content,
        tags if tags is not None else list(rec.tags),
        rec.kind,
        rec.key,
    )
    if looks_sensitive(new_content):
        await _audit_denied(
            workspace_id, actor, reason="sensitive_content", operation="update", target_record=str(rec.id)
        )
        raise MemoryStoreError(
            "content looks like a credential or secret and was not stored", "sensitive_content", 422
        )
    if expires_at is not _UNSET:
        if expires_at is not None and expires_at <= utcnow():
            raise MemoryStoreError("expires_at must be in the future", "invalid_expiry", 422)
        rec.expires_at = expires_at
    if new_content == rec.content and new_tags == list(rec.tags) and expires_at is _UNSET:
        return rec  # nothing changed: no new version
    rec.content, rec.tags = new_content, new_tags
    rec.content_sha256 = sha256_hex(new_content)
    rec.version += 1
    rec.updated_at, rec.updated_by = utcnow(), updated_by
    await session.flush()
    session.add(_version(rec, "update", actor))
    await _audit(session, workspace_id, "update", actor, rec.id, version=rec.version)
    await session.flush()
    return rec


async def delete_record(
    session: AsyncSession, workspace_id: uuid.UUID, record_id: uuid.UUID, *, actor: Actor
) -> MemoryRecord:
    """Soft delete: the record leaves reads and retrieval; its versions stay as audit history."""
    rec = await get_record(session, workspace_id, record_id, actor=actor, audit=False, lock=True)
    rec.deleted_at, rec.deleted_reason = utcnow(), "deleted"
    rec.version += 1
    await session.flush()
    session.add(_version(rec, "delete", actor))
    await _audit(session, workspace_id, "delete", actor, rec.id, version=rec.version)
    await session.flush()
    return rec


async def _expire(session: AsyncSession, rec: MemoryRecord, now: datetime) -> None:
    rec.deleted_at, rec.deleted_reason = now, "expired"
    rec.version += 1
    await session.flush()
    session.add(_version(rec, "expire", SYSTEM))
    await _audit(
        session,
        rec.workspace_id,
        "expire",
        SYSTEM,
        rec.id,
        version=rec.version,
        expired_at=str(rec.expires_at),
    )


async def expire_due(session: AsyncSession, *, now: datetime | None = None, limit: int = 500) -> int:
    """Record expiry for every record whose time has passed (idempotent; safe from several workers)."""
    now = now or utcnow()
    rows = (
        (
            await session.execute(
                select(MemoryRecord)
                .where(
                    MemoryRecord.deleted_at.is_(None),
                    MemoryRecord.expires_at.is_not(None),
                    MemoryRecord.expires_at <= now,
                )
                .with_for_update(skip_locked=True)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    for rec in rows:
        await _expire(session, rec, now)
    await session.flush()
    return len(rows)


async def expire_now(session: AsyncSession, workspace_id: uuid.UUID, record_ids: list[uuid.UUID]) -> int:
    """Fast-forward expiry for specific records that carry an ``expires_at`` (used by experiments to simulate elapsed time
    through the same code path as real expiry; it cannot touch records without an expiry or from another workspace)."""
    rows = (
        (
            await session.execute(
                select(MemoryRecord)
                .where(
                    MemoryRecord.id.in_(record_ids),
                    MemoryRecord.workspace_id == workspace_id,
                    MemoryRecord.deleted_at.is_(None),
                    MemoryRecord.expires_at.is_not(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for rec in rows:
        await _expire(session, rec, utcnow())
    return len(rows)


async def list_records(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    namespace: str | None = None,
    kind: str | None = None,
    tag: str | None = None,
    include_inactive: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[MemoryRecord]:
    q = select(MemoryRecord).where(MemoryRecord.workspace_id == workspace_id)
    if not include_inactive:
        q = q.where(*_live(utcnow()))
    if namespace:
        q = q.where(MemoryRecord.namespace == namespace)
    if kind:
        q = q.where(MemoryRecord.kind == kind)
    if tag:
        q = q.where(MemoryRecord.tags.contains([tag]))
    q = (
        q.order_by(MemoryRecord.namespace, MemoryRecord.key, MemoryRecord.created_at)
        .limit(limit)
        .offset(offset)
    )
    return list((await session.execute(q)).scalars())


async def history(
    session: AsyncSession, workspace_id: uuid.UUID, record_id: uuid.UUID
) -> list[MemoryVersion]:
    await get_record(session, workspace_id, record_id, actor=SYSTEM, audit=False, include_inactive=True)
    return list(
        (
            await session.execute(
                select(MemoryVersion)
                .where(MemoryVersion.record_id == record_id)
                .order_by(MemoryVersion.version, MemoryVersion.changed_at)
            )
        ).scalars()
    )


def provenance(rec: MemoryRecord) -> dict[str, Any]:
    return {
        **rec.source,
        "record_id": str(rec.id),
        "version": rec.version,
        "created_at": rec.created_at.isoformat(),
        "updated_at": rec.updated_at.isoformat(),
        "content_sha256": rec.content_sha256,
    }


async def search(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    query: str,
    *,
    actor: Actor,
    namespace: str | None = None,
    kinds: list[str] | None = None,
    tags: list[str] | None = None,
    limit: int = 5,
    audit: bool = True,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Keyword retrieval (PostgreSQL full-text) with metadata filters, relevance scores and provenance."""
    now = now or utcnow()
    tsq = build_tsquery(query)
    results: list[dict[str, Any]] = []
    if tsq is not None:
        q = func.to_tsquery(text("'english'"), tsq)
        rank = func.ts_rank_cd(MemoryRecord.search_vector, q).label("score")
        stmt = (
            select(MemoryRecord, rank)
            .where(
                MemoryRecord.workspace_id == workspace_id, MemoryRecord.search_vector.op("@@")(q), *_live(now)
            )
            .order_by(rank.desc(), MemoryRecord.key, MemoryRecord.created_at)
            .limit(max(1, min(limit, 50)))
        )
        if namespace:
            stmt = stmt.where(MemoryRecord.namespace == namespace)
        if kinds:
            stmt = stmt.where(MemoryRecord.kind.in_(kinds))
        for t in tags or []:
            stmt = stmt.where(MemoryRecord.tags.contains([t]))
        for rec, score in (await session.execute(stmt)).all():
            results.append(
                {
                    "id": str(rec.id),
                    "namespace": rec.namespace,
                    "kind": rec.kind,
                    "key": rec.key,
                    "content": rec.content,
                    "tags": list(rec.tags),
                    "version": rec.version,
                    "score": round(float(score), 6),
                    "provenance": provenance(rec),
                    "untrusted": True,
                }
            )
    if audit:
        await _audit(
            session,
            workspace_id,
            "search",
            actor,
            None,
            query_sha256=sha256_hex(query)[:16],
            namespace=namespace,
            returned=[r["id"] for r in results],
        )
    return results


async def audit_log(
    session: AsyncSession, workspace_id: uuid.UUID, *, record_id: uuid.UUID | None = None, limit: int = 100
) -> list[MemoryAuditEvent]:
    q = select(MemoryAuditEvent).where(MemoryAuditEvent.workspace_id == workspace_id)
    if record_id:
        q = q.where(MemoryAuditEvent.record_id == record_id)
    return list(
        (await session.execute(q.order_by(MemoryAuditEvent.created_at.desc()).limit(limit))).scalars()
    )
