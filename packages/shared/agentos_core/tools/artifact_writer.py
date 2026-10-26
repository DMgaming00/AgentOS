"""Write Markdown/JSON/text outputs into controlled artifact storage.

Safety properties:
* filenames are validated (no separators, no traversal) and storage paths are
  content-addressed under ``artifacts/<workspace>/<run>/``;
* writes are atomic (temp file + fsync + rename);
* the write is fenced: it happens inside a transaction holding the run lock and
  only if the caller's step lease is still valid;
* ``(run_id, idempotency_key)`` is unique, so retries and duplicate dispatches
  return the existing artifact instead of creating a second one.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from agentos_core.config import get_settings
from agentos_core.db.models import Artifact
from agentos_core.domain.errors import LeaseLostError, PolicyViolationError, ToolExecutionError
from agentos_core.engine.core import lease_valid
from agentos_core.engine.tx import lock_run, unit_of_work
from agentos_core.storage import validate_filename
from agentos_core.tools.base import SideEffect, Tool, ToolContext
from agentos_core.util import sha256_hex

CONTENT_TYPES = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".txt": "text/plain",
    ".csv": "text/csv",
}


class ArtifactWriterArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(description="File name such as report.md. Allowed extensions: .md .json .txt .csv")
    content: str | dict[str, Any] | list[Any] = Field(
        description="Full file content. Objects/arrays are serialized as JSON (for .json files)."
    )
    reason: str | None = Field(
        default=None, max_length=500, description="Why this artifact is being written."
    )

    @field_validator("filename")
    @classmethod
    def _filename(cls, v: str) -> str:
        try:
            validate_filename(v)
        except PolicyViolationError as exc:  # must surface as a ValidationError
            raise ValueError(exc.message) from None
        if not any(v.lower().endswith(ext) for ext in CONTENT_TYPES):
            raise ValueError(f"extension must be one of {sorted(CONTENT_TYPES)}")
        return v

    @field_validator("content", mode="after")
    @classmethod
    def _size(cls, v: str | dict[str, Any] | list[Any]) -> str:
        if not isinstance(v, str):
            v = json.dumps(v, indent=2, sort_keys=True, default=str)
        limit = get_settings().max_artifact_bytes
        if len(v.encode()) > limit:
            raise ValueError(f"content exceeds {limit} bytes")
        return v


def content_type(filename: str) -> str:
    return next(ct for ext, ct in CONTENT_TYPES.items() if filename.lower().endswith(ext))


class ArtifactWriterTool(Tool):
    name = "artifact_writer"
    description = (
        "Save a Markdown, JSON, text or CSV file as a downloadable artifact of this run. "
        "Requires human approval before it executes."
    )
    args_model = ArtifactWriterArgs
    permission = "write"
    side_effect = SideEffect.INTERNAL
    idempotent = True
    requires_approval = True
    timeout_seconds = 15.0

    def describe_action(self, args: BaseModel) -> tuple[str, str | None]:
        assert isinstance(args, ArtifactWriterArgs)
        size = len(str(args.content).encode())
        return f"Write artifact {args.filename} ({size} bytes)", f"artifact:{args.filename}"

    async def run(self, args: ArtifactWriterArgs, ctx: ToolContext) -> dict[str, Any]:  # type: ignore[override]
        if args.filename.lower().endswith(".json"):
            try:
                json.loads(str(args.content))
            except json.JSONDecodeError as exc:
                raise ToolExecutionError(f"content is not valid JSON: {exc}") from exc
        assert isinstance(args.content, str)  # normalized by the validator
        data = args.content.encode()
        digest = sha256_hex(data)
        async with unit_of_work() as session:
            await lock_run(session, ctx.run_id)
            if not await lease_valid(session, ctx.step_id, ctx.lease_token):
                raise LeaseLostError("lease lost before artifact write")
            existing = (
                await session.execute(
                    select(Artifact).where(
                        Artifact.run_id == ctx.run_id, Artifact.idempotency_key == ctx.idempotency_key
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return _describe(existing, deduplicated=True)
            clash = (
                await session.execute(
                    select(Artifact.id).where(
                        Artifact.run_id == ctx.run_id, Artifact.filename == args.filename
                    )
                )
            ).scalar_one_or_none()
            if clash is not None:
                raise ToolExecutionError(f"an artifact named {args.filename!r} already exists in this run")
            rel = f"artifacts/{ctx.workspace_id}/{ctx.run_id}/{digest[:16]}-{args.filename}"
            ctx.storage.write_atomic(rel, data)
            artifact_id = uuid.uuid4()
            await session.execute(
                insert(Artifact)
                .values(
                    id=artifact_id,
                    run_id=ctx.run_id,
                    workspace_id=ctx.workspace_id,
                    step_id=ctx.step_id,
                    tool_execution_id=ctx.tool_execution_id,
                    filename=args.filename,
                    content_type=content_type(args.filename),
                    storage_path=rel,
                    sha256=digest,
                    byte_size=len(data),
                    idempotency_key=ctx.idempotency_key,
                )
                .on_conflict_do_nothing(index_elements=["run_id", "idempotency_key"])
            )
            row = (
                await session.execute(
                    select(Artifact).where(
                        Artifact.run_id == ctx.run_id, Artifact.idempotency_key == ctx.idempotency_key
                    )
                )
            ).scalar_one()
            return _describe(row, deduplicated=row.id != artifact_id)


def _describe(a: Artifact, *, deduplicated: bool) -> dict[str, Any]:
    return {
        "artifact_id": str(a.id),
        "filename": a.filename,
        "content_type": a.content_type,
        "sha256": a.sha256,
        "byte_size": a.byte_size,
        "deduplicated": deduplicated,
    }
