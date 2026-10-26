"""Autopilot demo assets: a deliberately flawed extraction agent, OpenEval datasets, a live workflow."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import openeval
from agentos_core.autopilot.evaluation import import_dataset
from agentos_core.autopilot.versions import initialize_agent_versioning
from agentos_core.db.models import AgentDefinition, WorkflowDefinition, Workspace

DATASETS = Path(__file__).resolve().parents[4] / "examples" / "autopilot" / "datasets"
EXTRACTION_AGENT = "Extraction Agent"

# The flaw (reproducible, prompt-level): asking for "standard currency notation" makes the agent
# emit prices as strings like "$0.95", which violates the numeric output contract.
FLAWED_INSTRUCTIONS = (
    "You extract metrics from technical documents. Search the documents for the requested subject, then report "
    "each requested value as written in the source, keeping prices in standard currency notation (for example "
    "$1.20). Cite the document section you used for every value."
)


def live_extraction_workflow() -> dict[str, Any]:
    """Production-style task: compare two platforms in one step (needs one search per subject)."""
    fields = [
        {"key": "nimbus_p95_latency_ms", "label": "p95 latency", "unit": "ms", "subject": "Nimbus Inference"},
        {"key": "helix_p95_latency_ms", "label": "p95 latency", "unit": "ms", "subject": "Helix Serve"},
    ]
    return {
        "nodes": [
            {
                "key": "extract",
                "type": "agent_task",
                "agent": EXTRACTION_AGENT,
                "name": "Compare p95 latency",
                "prompt": "Extract the p95 latency of Nimbus Inference and Helix Serve from the workspace documents.",
                "input": {"fields": fields, "documents": ["platform-nimbus.md", "platform-helix.md"]},
                "retry": {"max_attempts": 1},
                "output_contract": {
                    "type": "object",
                    "required": [f["key"] for f in fields],
                    "properties": {f["key"]: {"type": "number"} for f in fields},
                },
            }
        ],
        "budget": {"max_model_calls": 10, "max_cost_usd": 0.5, "max_concurrency": 1},
    }


async def seed_autopilot(session: AsyncSession, ws: Workspace, *, admin_id: uuid.UUID) -> None:
    agent = (
        await session.execute(
            select(AgentDefinition).where(
                AgentDefinition.workspace_id == ws.id, AgentDefinition.name == EXTRACTION_AGENT
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        agent = AgentDefinition(
            id=uuid.uuid4(),
            workspace_id=ws.id,
            name=EXTRACTION_AGENT,
            description="Extracts numeric facts with citations. Seeded with a deliberate formatting flaw for the "
            "Autopilot demo.",
            system_instructions=FLAWED_INSTRUCTIONS,
            provider="mock",
            model="mock-extractor",
            model_configuration={"scenario": "extractor"},
            allowed_tool_ids=["document_search", "structured_data_reader"],
            max_steps=6,
            max_cost_usd=Decimal("0.5"),
            timeout_seconds=120,
        )
        session.add(agent)
        await session.flush()
        await initialize_agent_versioning(session, agent, created_by=admin_id)
    settings = dict(ws.settings or {})
    if "autopilot" not in settings:
        settings["autopilot"] = {"allowed_models": [{"provider": "mock", "model": "mock-extractor"}]}
        ws.settings = settings
    if DATASETS.exists():
        for path in sorted(DATASETS.glob("*.json")):
            ds = openeval.Dataset.model_validate(json.loads(path.read_text()))
            await import_dataset(session, ws.id, ds, created_by=admin_id)
    exists = (
        await session.execute(
            select(WorkflowDefinition.id).where(
                WorkflowDefinition.workspace_id == ws.id, WorkflowDefinition.name == "metric-extraction-live"
            )
        )
    ).first()
    if exists is None:
        session.add(
            WorkflowDefinition(
                id=uuid.uuid4(),
                workspace_id=ws.id,
                name="metric-extraction-live",
                version=1,
                description="Live extraction comparing two platforms (monitored after deployments)",
                definition=live_extraction_workflow(),
                created_by=admin_id,
            )
        )
    await session.flush()
