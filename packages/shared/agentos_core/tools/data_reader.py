"""Read CSV/JSON data from workspace documents or this workspace's artifacts."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import jsonschema
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from agentos_core.config import get_settings
from agentos_core.db.models import Artifact, Document
from agentos_core.db.session import get_sessionmaker
from agentos_core.domain.errors import InvalidInputError, ToolExecutionError
from agentos_core.storage import validate_filename
from agentos_core.tools.base import Tool, ToolContext


class DataReaderArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(
        max_length=121, description="Workspace document or artifact filename (.csv or .json)."
    )
    source: str = Field(default="document", pattern="^(document|artifact)$")
    max_rows: int = Field(default=200, ge=1, le=1000)
    json_schema: dict[str, Any] | None = Field(
        default=None, description="Optional JSON Schema the parsed data must satisfy."
    )


def parse_structured(filename: str, data: bytes, max_rows: int) -> dict[str, Any]:
    text = data.decode("utf-8")
    if filename.lower().endswith(".csv"):
        reader = csv.DictReader(io.StringIO(text))
        rows: list[dict[str, Any]] = []
        for i, row in enumerate(reader):
            if i >= max_rows:
                break
            rows.append({k: _coerce(v) for k, v in row.items() if k is not None})
        return {
            "format": "csv",
            "columns": reader.fieldnames or [],
            "rows": rows,
            "truncated": len(rows) >= max_rows,
        }
    if filename.lower().endswith(".json"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolExecutionError(f"invalid JSON: {exc}") from exc
        truncated = False
        if isinstance(value, list) and len(value) > max_rows:
            value, truncated = value[:max_rows], True
        return {"format": "json", "data": value, "truncated": truncated}
    raise InvalidInputError("only .csv and .json files can be read")


def _coerce(v: str | None) -> Any:
    if v is None:
        return None
    s = v.strip()
    for cast in (int, float):
        try:
            return cast(s)
        except ValueError:
            continue
    return s


class DataReaderTool(Tool):
    name = "structured_data_reader"
    description = "Read a CSV or JSON file from the workspace's documents or artifacts, with size limits."
    args_model = DataReaderArgs
    timeout_seconds = 10.0

    async def run(self, args: DataReaderArgs, ctx: ToolContext) -> dict[str, Any]:  # type: ignore[override]
        validate_filename(args.filename)
        limit = get_settings().max_data_file_bytes
        async with get_sessionmaker()() as session:
            if args.source == "document":
                row = (
                    await session.execute(
                        select(Document.storage_path, Document.byte_size).where(
                            Document.workspace_id == ctx.workspace_id, Document.filename == args.filename
                        )
                    )
                ).first()
            else:
                row = (
                    await session.execute(
                        select(Artifact.storage_path, Artifact.byte_size)
                        .where(Artifact.workspace_id == ctx.workspace_id, Artifact.filename == args.filename)
                        .order_by(Artifact.created_at.desc())
                    )
                ).first()
        if row is None or row[0] is None:
            raise ToolExecutionError(f"{args.source} {args.filename!r} not found in this workspace")
        if row[1] > limit:
            raise ToolExecutionError(f"file is {row[1]} bytes; limit is {limit}")
        parsed = parse_structured(
            args.filename, ctx.storage.read_bytes(row[0], max_bytes=limit), args.max_rows
        )
        if args.json_schema is not None:
            payload = parsed.get("rows", parsed.get("data"))
            try:
                jsonschema.validate(payload, args.json_schema)
            except jsonschema.ValidationError as exc:
                raise ToolExecutionError(f"data does not match schema: {exc.message}") from exc
            except jsonschema.SchemaError as exc:
                raise InvalidInputError(f"invalid json_schema: {exc.message}") from exc
        return {"filename": args.filename, **parsed}
