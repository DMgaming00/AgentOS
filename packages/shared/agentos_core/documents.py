"""Document ingestion: text/Markdown/PDF/CSV/JSON extraction, chunking and indexing.

Documents are untrusted input. Their text is stored and searched, but content is
always presented to models as quoted data, never as instructions.
"""

from __future__ import annotations

import io
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import Document, DocumentChunk
from agentos_core.domain.errors import InvalidInputError
from agentos_core.storage import Storage, validate_filename
from agentos_core.util import sha256_hex

TEXT_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".json": "application/json",
    ".pdf": "application/pdf",
}
MAX_CHUNK_CHARS = 1200


@dataclass
class Chunk:
    section: str
    content: str


def content_type_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in TEXT_TYPES:
        raise InvalidInputError(f"unsupported document type {ext!r}; allowed: {sorted(TEXT_TYPES)}")
    return TEXT_TYPES[ext]


def extract_text(filename: str, data: bytes) -> str:
    ctype = content_type_for(filename)
    if ctype == "application/pdf":
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            pages = [
                f"## Page {i + 1}\n\n{(p.extract_text() or '').strip()}" for i, p in enumerate(reader.pages)
            ]
        except Exception as exc:
            raise InvalidInputError(f"could not parse PDF: {exc}") from exc
        return "\n\n".join(pages)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidInputError("document must be UTF-8 text") from exc


def chunk_markdown(text: str, *, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """Split on Markdown headings, then on paragraphs to respect ``max_chars``.

    The section label is the heading path (e.g. ``Pricing > GPU tiers``), which is
    what citations reference.
    """
    chunks: list[Chunk] = []
    heading_stack: list[tuple[int, str]] = []
    buf: list[str] = []

    def section() -> str:
        return " > ".join(h for _, h in heading_stack)

    def flush() -> None:
        body = "\n".join(buf).strip()
        buf.clear()
        if not body:
            return
        paragraphs = re.split(r"\n\s*\n", body)
        current = ""
        for para in paragraphs:
            if current and len(current) + len(para) + 2 > max_chars:
                chunks.append(Chunk(section(), current.strip()))
                current = ""
            while len(para) > max_chars:
                chunks.append(Chunk(section(), para[:max_chars]))
                para = para[max_chars:]
            current = f"{current}\n\n{para}" if current else para
        if current.strip():
            chunks.append(Chunk(section(), current.strip()))

    for line in text.splitlines():
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            flush()
            level = len(m.group(1))
            heading_stack[:] = [h for h in heading_stack if h[0] < level]
            heading_stack.append((level, m.group(2).strip()))
        else:
            buf.append(line)
    flush()
    return chunks


def _title(filename: str, text: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
    return (m.group(1).strip() if m else Path(filename).stem)[:500]


async def ingest_document(
    session: AsyncSession, *, workspace_id: uuid.UUID, filename: str, data: bytes, storage: Storage
) -> Document:
    """Store (or replace) a workspace document and rebuild its chunks."""
    validate_filename(filename)
    ctype = content_type_for(filename)
    text = extract_text(filename, data)
    digest = sha256_hex(data)
    rel = f"documents/{workspace_id}/{digest}-{filename}"
    storage.write_atomic(rel, data)

    doc = (
        await session.execute(
            select(Document).where(Document.workspace_id == workspace_id, Document.filename == filename)
        )
    ).scalar_one_or_none()
    if doc is None:
        doc = Document(id=uuid.uuid4(), workspace_id=workspace_id, filename=filename)
        session.add(doc)
    doc.title = _title(filename, text)
    doc.content_type = ctype
    doc.storage_path = rel
    doc.sha256 = digest
    doc.byte_size = len(data)
    await session.flush()
    await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc.id))
    if ctype in ("text/markdown", "text/plain", "application/pdf"):
        for i, chunk in enumerate(chunk_markdown(text)):
            session.add(
                DocumentChunk(
                    document_id=doc.id,
                    workspace_id=workspace_id,
                    chunk_index=i,
                    section=chunk.section[:500],
                    content=chunk.content,
                )
            )
    await session.flush()
    return doc
