"""The Arena corpus on disk (examples/arena) and its import into a workspace.

The builder (``scripts/build_arena_corpus.py``) writes dataset files plus ``MANIFEST.json`` with a content hash per dataset.
Import refuses a dataset whose recomputed hash differs from the manifest, so a hand-edited file cannot enter the Arena
unnoticed, and ``corpus_status`` re-checks what is stored in the database against the manifest at any time.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import openeval
from agentos_core.autopilot.evaluation import EvaluationError, import_dataset
from agentos_core.db.models import Document, EvaluationDataset

ARENA_DIR = Path(__file__).resolve().parents[4] / "examples" / "arena"
FAMILIES = ("extraction", "calculation", "grounded_qa")
SELECTION_SPLITS = ("development", "validation")
FAMILY_LABEL = {
    "extraction": "Numeric extraction",
    "calculation": "Calculation",
    "grounded_qa": "Grounded Q&A",
}


def load_manifest() -> dict[str, Any]:
    path = ARENA_DIR / "MANIFEST.json"
    if not path.exists():
        raise EvaluationError(
            "the Arena corpus is not installed (examples/arena/MANIFEST.json)", code="no_corpus"
        )
    manifest: dict[str, Any] = json.loads(path.read_text())
    return manifest


def load_datasets() -> list[tuple[dict[str, Any], openeval.Dataset]]:
    """(manifest entry, dataset) for every dataset in the manifest, hash-verified."""
    out = []
    for entry in load_manifest()["datasets"]:
        ds = openeval.Dataset.model_validate(json.loads((ARENA_DIR / "datasets" / entry["file"]).read_text()))
        if ds.content_hash() != entry["hash"]:
            raise EvaluationError(
                f"{entry['file']} does not match its manifest hash; rebuild with scripts/build_arena_corpus.py",
                code="corpus_hash_mismatch",
            )
        out.append((entry, ds))
    return out


async def import_corpus(
    session: AsyncSession, workspace_id: uuid.UUID, *, created_by: uuid.UUID | None
) -> dict[tuple[str, str], EvaluationDataset]:
    """Idempotently store the corpus; returns (family, split) -> stored dataset."""
    stored: dict[tuple[str, str], EvaluationDataset] = {}
    for entry, ds in load_datasets():
        row, _ = await import_dataset(session, workspace_id, ds, created_by=created_by)
        stored[(entry["family"], entry["split"])] = row
    return stored


async def corpus_status(session: AsyncSession, workspace_id: uuid.UUID) -> dict[str, Any]:
    manifest = load_manifest()
    rows = {
        (d.name, d.split, d.version): d
        for d in (
            await session.execute(
                select(EvaluationDataset).where(EvaluationDataset.workspace_id == workspace_id)
            )
        ).scalars()
    }
    datasets = []
    for entry in manifest["datasets"]:
        stored = rows.get((entry["name"], entry["split"], entry["version"]))
        datasets.append(
            {
                "name": entry["name"],
                "family": entry["family"],
                "split": entry["split"],
                "version": entry["version"],
                "cases": entry["cases"],
                "manifest_hash": entry["hash"],
                "stored_hash": stored.dataset_hash if stored else None,
                "state": "missing"
                if stored is None
                else ("verified" if stored.dataset_hash == entry["hash"] else "mismatch"),
                "kinds": entry["kinds"],
            }
        )
    have = {
        name
        for (name,) in (
            await session.execute(select(Document.filename).where(Document.workspace_id == workspace_id))
        ).all()
    }
    return {
        "corpus": manifest["corpus"],
        "version": manifest["version"],
        "total_cases": manifest["total_cases"],
        "split_salt": manifest["split_salt"],
        "datasets": datasets,
        "leakage": manifest["leakage"],
        "source_documents": [
            {"filename": f, "sha256": h, "present_in_workspace": f in have}
            for f, h in sorted(manifest["source_documents"].items())
        ],
        "all_verified": all(d["state"] == "verified" for d in datasets),
    }
