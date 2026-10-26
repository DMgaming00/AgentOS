from __future__ import annotations

import uuid

from fastapi import APIRouter, File, Query, UploadFile
from sqlalchemy import func, select

from agentos.dependencies import SessionDep, WorkspaceDep, api_error, rate_limited
from agentos.schemas.api import DocumentOut, Page
from agentos_core.config import get_settings
from agentos_core.db.models import Document, DocumentChunk
from agentos_core.documents import ingest_document
from agentos_core.domain.errors import AgentOSError
from agentos_core.domain.states import Role
from agentos_core.storage import Storage

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])


async def _out(session: SessionDep, doc: Document) -> DocumentOut:
    n = (await session.execute(select(func.count()).where(DocumentChunk.document_id == doc.id))).scalar_one()
    return DocumentOut.model_validate(doc).model_copy(update={"chunk_count": n})


@router.get("", response_model=Page[DocumentOut])
async def list_documents(
    session: SessionDep,
    ctx: WorkspaceDep,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> Page[DocumentOut]:
    base = select(Document).where(Document.workspace_id == ctx.workspace_id)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (await session.execute(base.order_by(Document.filename).limit(limit).offset(offset))).scalars().all()
    )
    return Page(items=[await _out(session, d) for d in rows], total=total, limit=limit, offset=offset)


@router.post("", response_model=DocumentOut, status_code=201, dependencies=[rate_limited("critical")])
async def upload_document(
    session: SessionDep, ctx: WorkspaceDep, file: UploadFile = File(...)
) -> DocumentOut:
    ctx.require(Role.DEVELOPER)
    limit = get_settings().max_upload_bytes
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise api_error(413, "too_large", f"upload exceeds {limit} bytes")
    try:
        doc = await ingest_document(
            session, workspace_id=ctx.workspace_id, filename=file.filename or "", data=data, storage=Storage()
        )
    except AgentOSError as exc:
        raise api_error(422, exc.error_type, exc.message) from None
    return await _out(session, doc)


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> DocumentOut:
    doc = await session.get(Document, document_id)
    if doc is None or doc.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "document not found")
    return await _out(session, doc)
