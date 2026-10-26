from __future__ import annotations

import uuid

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, select

from agentos.api.runs import get_run_for
from agentos.dependencies import SessionDep, WorkspaceDep, api_error
from agentos.schemas.api import ArtifactOut, Page
from agentos_core.db.models import Artifact
from agentos_core.domain.errors import PolicyViolationError
from agentos_core.storage import Storage

router = APIRouter(prefix="/api/v1", tags=["artifacts"])
PREVIEW_LIMIT = 512 * 1024


async def _get(session: SessionDep, ctx: WorkspaceDep, artifact_id: uuid.UUID) -> Artifact:
    a = await session.get(Artifact, artifact_id)
    if a is None or a.workspace_id != ctx.workspace_id:
        raise api_error(404, "not_found", "artifact not found")
    return a


@router.get("/artifacts", response_model=Page[ArtifactOut])
async def list_artifacts(
    session: SessionDep, ctx: WorkspaceDep, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)
) -> Page[ArtifactOut]:
    base = select(Artifact).where(Artifact.workspace_id == ctx.workspace_id)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        await session.execute(base.order_by(Artifact.created_at.desc()).limit(limit).offset(offset))
    ).scalars()
    return Page(items=[ArtifactOut.model_validate(a) for a in rows], total=total, limit=limit, offset=offset)


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactOut])
async def run_artifacts(run_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> list[ArtifactOut]:
    await get_run_for(session, ctx, run_id)
    rows = (
        await session.execute(select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at))
    ).scalars()
    return [ArtifactOut.model_validate(a) for a in rows]


@router.get("/artifacts/{artifact_id}", response_model=ArtifactOut)
async def get_artifact(artifact_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> ArtifactOut:
    return ArtifactOut.model_validate(await _get(session, ctx, artifact_id))


def _path(a: Artifact):  # type: ignore[no-untyped-def]
    try:
        path = Storage().resolve(a.storage_path)
    except PolicyViolationError:
        raise api_error(500, "storage_error", "artifact path is invalid") from None
    if not path.exists():
        raise api_error(410, "gone", "artifact content is missing from storage")
    return path


@router.get("/artifacts/{artifact_id}/download", response_class=FileResponse)
async def download(artifact_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> FileResponse:
    a = await _get(session, ctx, artifact_id)
    return FileResponse(
        _path(a),
        media_type=a.content_type,
        filename=a.filename,
        headers={
            "X-Content-Type-Options": "nosniff",
            "ETag": f'"{a.sha256}"',
            "Content-Security-Policy": "default-src 'none'",
        },
    )


@router.get("/artifacts/{artifact_id}/content", response_class=PlainTextResponse)
async def content(artifact_id: uuid.UUID, session: SessionDep, ctx: WorkspaceDep) -> PlainTextResponse:
    """Text preview (always served as text/plain so it can never execute in the browser)."""
    a = await _get(session, ctx, artifact_id)
    data = _path(a).read_bytes()[:PREVIEW_LIMIT]
    return PlainTextResponse(
        data.decode("utf-8", errors="replace"), headers={"X-Content-Type-Options": "nosniff"}
    )
