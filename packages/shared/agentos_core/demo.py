"""Demo agents, workflows and sample data (used by the seed script and tests).

By default every agent uses the deterministic mock provider. Set
``AGENTOS_DEMO_PROVIDER=anthropic`` (or ``openai``) before seeding to create the
same agents against a real model; the system instructions are written for real models.
"""

from __future__ import annotations

import os
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.versions import initialize_agent_versioning
from agentos_core.config import get_settings
from agentos_core.db.models import AgentDefinition, User, WorkflowDefinition, Workspace, WorkspaceMember
from agentos_core.documents import ingest_document
from agentos_core.security_passwords import hash_password
from agentos_core.storage import Storage
from agentos_core.util import utcnow

REPO_ROOT = Path(__file__).resolve().parents[3]
SAMPLE_DOCS = REPO_ROOT / "examples" / "research-report" / "documents"

DEV_USERS = [
    ("admin@agentos.dev", "admin", "agentos-admin"),
    ("developer@agentos.dev", "developer", "agentos-dev"),
    ("viewer@agentos.dev", "viewer", "agentos-viewer"),
]

AGENTS: list[dict[str, Any]] = [
    {
        "name": "Research Agent",
        "scenario": "research",
        "description": "Retrieves and summarizes source-backed facts from workspace documents.",
        "tools": ["document_search", "structured_data_reader"],
        "instructions": (
            "You are a research agent. Use document_search to find information about the subject given in your "
            "input. Extract factual statements and every quantitative metric you find, each with its citation "
            "exactly as returned (document#section). Do not invent facts; if something is not in the documents, "
            "say it is missing. If two sources disagree, report both values as a conflict. Return JSON with keys: "
            "subject, summary, findings[{statement, citation}], metrics{metric_key: {label, value, unit, citation}}, "
            "conflicts[], sources[]."
        ),
    },
    {
        "name": "Analysis Agent",
        "scenario": "analysis",
        "description": "Compares source-backed findings and performs numerical analysis with the calculator.",
        "tools": ["calculator", "structured_data_reader"],
        "instructions": (
            "You are an analysis agent. Compare the research outputs in 'upstream'. For every metric reported by at "
            "least two subjects, use the calculator tool for means and ratios (never compute numbers yourself). "
            "Identify missing and conflicting evidence. Return JSON with keys: subjects[], comparison[{metric, "
            "label, unit, values, best, direction, mean, spread_ratio, citations}], missing_evidence[], conflicts[], "
            "observations[] (each observation must cite a source)."
        ),
    },
    {
        "name": "Writer Agent",
        "scenario": "writer",
        "description": "Writes the final Markdown report and exports it as an artifact (approval required).",
        "tools": ["artifact_writer"],
        "instructions": (
            "You are a technical writer. Write a structured Markdown report from the analysis in 'upstream' with "
            "sections: Summary, Method, Numerical comparison (table), Observations (source-backed, with citations), "
            "Conclusions (clearly labeled as interpretation), Missing or conflicting evidence, References. Save it "
            "with the artifact_writer tool using the filename from your input. If the reviewer rejects the export, "
            "revise once using their feedback. Finally return JSON {status, artifact}."
        ),
        "extra": {"max_revisions": 1},
    },
    {
        "name": "Processing Agent",
        "scenario": "echo",
        "description": "Deterministic processing step used by the failure-recovery demo and benchmarks.",
        "tools": [],
        "instructions": "Process the provided input and return a JSON summary of it.",
    },
    {
        "name": "Untrusted Agent",
        "scenario": "forbidden_tool",
        "description": "Attempts to call a tool it is not allowed to use (permission-enforcement demo).",
        "tools": ["calculator"],
        "instructions": "You only have the calculator tool. Return JSON describing what you did.",
    },
]


def research_report_workflow() -> dict[str, Any]:
    subjects = [
        ("research_nimbus", "Nimbus Inference"),
        ("research_helix", "Helix Serve"),
        ("research_vectorforge", "Vector Forge"),
    ]
    research_nodes = [
        {
            "key": key,
            "type": "agent_task",
            "agent": "Research Agent",
            "name": f"Research {name}",
            "tools": ["document_search"],
            "input": {"platform": name},
            "retry": {"max_attempts": 3, "backoff_base_seconds": 1, "backoff_max_seconds": 10},
            "timeout_seconds": 180,
        }
        for key, name in subjects
    ]
    keys = [k for k, _ in subjects]
    return {
        "nodes": [
            *research_nodes,
            {
                "key": "analyze",
                "type": "agent_task",
                "agent": "Analysis Agent",
                "name": "Compare platforms",
                "depends_on": keys,
                "tools": ["calculator"],
                "timeout_seconds": 180,
            },
            {
                "key": "write_report",
                "type": "agent_task",
                "agent": "Writer Agent",
                "name": "Write and export report",
                "depends_on": ["analyze", *keys],
                "tools": ["artifact_writer"],
                "timeout_seconds": 180,
                "input": {
                    "filename": "inference-platform-comparison.md",
                    "title": "AI Inference Platform Comparison",
                    "research": {k: {"$ref": f"steps.{k}.output"} for k in keys},
                },
            },
        ],
        "budget": {
            "max_model_calls": 40,
            "max_tool_calls": 60,
            "max_cost_usd": 2.0,
            "max_concurrency": 3,
            "max_duration_seconds": 3600,
        },
    }


def failure_recovery_workflow() -> dict[str, Any]:
    retry = {"max_attempts": 3, "backoff_base_seconds": 0.5, "backoff_max_seconds": 2}
    return {
        "nodes": [
            {
                "key": "fetch_input",
                "type": "tool_task",
                "tool": "structured_data_reader",
                "name": "Fetch sample input",
                "arguments": {"filename": "benchmarks.csv"},
            },
            {
                "key": "process_a",
                "type": "agent_task",
                "agent": "Processing Agent",
                "name": "Process A",
                "depends_on": ["fetch_input"],
                "retry": retry,
                "tools": [],
                "input": {"rows": {"$ref": "steps.fetch_input.output.rows"}, "shard": "A"},
                # Deterministic fault: the worker process running attempt 1 is killed mid-step.
                "inject": {"sleep_seconds": 2, "crash_worker_on_attempt": 1, "crash_after_seconds": 1},
            },
            {
                "key": "process_b",
                "type": "agent_task",
                "agent": "Processing Agent",
                "name": "Process B",
                "depends_on": ["fetch_input"],
                "retry": retry,
                "tools": [],
                "input": {"rows": {"$ref": "steps.fetch_input.output.rows"}, "shard": "B"},
                "inject": {"sleep_seconds": 1},
            },
            {
                "key": "aggregate",
                "type": "aggregation",
                "mode": "collect",
                "name": "Aggregate",
                "depends_on": ["process_a", "process_b"],
            },
            {
                "key": "export",
                "type": "tool_task",
                "tool": "artifact_writer",
                "name": "Export results",
                "depends_on": ["aggregate"],
                "arguments": {
                    "filename": "recovery-results.json",
                    "content": {"$ref": "steps.aggregate.output"},
                    "reason": "Export aggregated results",
                },
            },
        ],
        "budget": {
            "max_model_calls": 20,
            "max_cost_usd": 1.0,
            "max_concurrency": 4,
            "max_duration_seconds": 900,
        },
    }


def approval_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "key": "draft_note",
                "type": "agent_task",
                "agent": "Writer Agent",
                "name": "Draft and export note",
                "tools": ["artifact_writer"],
                "retry": {"max_attempts": 3, "backoff_base_seconds": 0.5},
                "input": {"filename": "approved-note.md", "title": "Permission enforcement demo"},
                # Crash right after the approved write: the retry must reuse the artifact, not create a second one.
                "inject": {"crash_after_tool": "artifact_writer"},
            },
            {
                "key": "untrusted_attempt",
                "type": "agent_task",
                "agent": "Untrusted Agent",
                "name": "Unauthorized tool attempt",
                "tools": ["calculator"],
            },
        ],
        "budget": {
            "max_model_calls": 20,
            "max_cost_usd": 1.0,
            "max_concurrency": 2,
            "max_duration_seconds": 3600,
        },
    }


WORKFLOWS = {
    "research-report": (
        "Multi-agent research report: research (parallel) -> analyze -> write -> approval -> export",
        research_report_workflow,
    ),
    "failure-recovery": (
        "Worker crash and lease-based recovery with fenced retries",
        failure_recovery_workflow,
    ),
    "approval-workflow": (
        "Approval rejection, resubmission and idempotent export; denied unauthorized tool",
        approval_workflow,
    ),
}


def agent_model(provider: str, scenario: str) -> tuple[str, dict[str, Any]]:
    settings = get_settings()
    if provider == "anthropic":
        return settings.anthropic_model, {"effort": "medium"}
    if provider == "openai":
        if not settings.openai_model:
            raise RuntimeError("AGENTOS_OPENAI_MODEL must be set to seed OpenAI agents")
        return settings.openai_model, {}
    return f"mock-{scenario}", {"scenario": scenario}


async def seed(
    session: AsyncSession, *, storage: Storage | None = None, provider: str | None = None
) -> Workspace:
    """Idempotently create dev users, the demo workspace, agents, documents and workflows."""
    provider = provider or os.environ.get("AGENTOS_DEMO_PROVIDER", "mock")
    users: dict[str, User] = {}
    for email, role, password in DEV_USERS:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
        if user is None:
            user = User(id=uuid.uuid4(), email=email, password_hash=hash_password(password), role=role)
            session.add(user)
        users[role] = user
    await session.flush()

    ws = (
        await session.execute(select(Workspace).where(Workspace.name == "Demo Workspace"))
    ).scalar_one_or_none()
    if ws is None:
        ws = Workspace(
            id=uuid.uuid4(),
            name="Demo Workspace",
            owner_id=users["admin"].id,
            settings={"max_run_cost_usd": 5.0},
        )
        session.add(ws)
        await session.flush()
    for role, user in users.items():
        member = await session.get(WorkspaceMember, (ws.id, user.id))
        if member is None:
            session.add(WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=role))

    for spec in AGENTS:
        existing = (
            await session.execute(
                select(AgentDefinition).where(
                    AgentDefinition.workspace_id == ws.id, AgentDefinition.name == spec["name"]
                )
            )
        ).scalar_one_or_none()
        agent_provider = provider if spec["scenario"] in ("research", "analysis", "writer") else "mock"
        model, config = agent_model(agent_provider, spec["scenario"])
        config = {**config, **spec.get("extra", {})}
        if existing is None:
            agent = AgentDefinition(
                id=uuid.uuid4(),
                workspace_id=ws.id,
                name=spec["name"],
                description=spec["description"],
                system_instructions=spec["instructions"],
                provider=agent_provider,
                model=model,
                model_configuration=config,
                allowed_tool_ids=spec["tools"],
                max_steps=spec.get("max_steps", 8),
                max_cost_usd=Decimal("1.0"),
                timeout_seconds=300,
            )
            session.add(agent)
            await session.flush()
            await initialize_agent_versioning(session, agent, created_by=users["admin"].id)
        elif existing.active_version_id is None:
            await initialize_agent_versioning(session, existing, created_by=users["admin"].id)
    await session.flush()

    storage = storage or Storage()
    if SAMPLE_DOCS.exists():
        for path in sorted(SAMPLE_DOCS.iterdir()):
            if path.is_file():
                await ingest_document(
                    session, workspace_id=ws.id, filename=path.name, data=path.read_bytes(), storage=storage
                )

    for name, (description, factory) in WORKFLOWS.items():
        exists = (
            await session.execute(
                select(WorkflowDefinition.id).where(
                    WorkflowDefinition.workspace_id == ws.id, WorkflowDefinition.name == name
                )
            )
        ).first()
        if exists is None:
            session.add(
                WorkflowDefinition(
                    id=uuid.uuid4(),
                    workspace_id=ws.id,
                    name=name,
                    description=description,
                    version=1,
                    definition=factory(),
                    created_by=users["admin"].id,
                )
            )
    await session.flush()
    from agentos_core.autopilot.demo import seed_autopilot

    await seed_autopilot(session, ws, admin_id=users["admin"].id)
    await seed_lab(session, users, storage)
    return ws


async def seed_lab(session: AsyncSession, users: dict[str, User], storage: Storage) -> Workspace:
    """'Autopilot Lab': an isolated test environment (own documents, datasets, deployments)."""
    lab = (
        await session.execute(select(Workspace).where(Workspace.name == "Autopilot Lab"))
    ).scalar_one_or_none()
    if lab is None:
        # Explicitly later than the demo workspace (same transaction => same now()), so it is never the default.
        lab = Workspace(
            id=uuid.uuid4(),
            name="Autopilot Lab",
            owner_id=users["admin"].id,
            settings={"max_run_cost_usd": 5.0, "environment": "test"},
            created_at=utcnow() + timedelta(seconds=1),
        )
        session.add(lab)
        await session.flush()
    for role in ("admin", "developer"):
        if await session.get(WorkspaceMember, (lab.id, users[role].id)) is None:
            session.add(WorkspaceMember(workspace_id=lab.id, user_id=users[role].id, role=role))
    if SAMPLE_DOCS.exists():
        for path in sorted(SAMPLE_DOCS.iterdir()):
            if path.is_file():
                await ingest_document(
                    session, workspace_id=lab.id, filename=path.name, data=path.read_bytes(), storage=storage
                )
    from agentos_core.autopilot.demo import seed_autopilot

    await seed_autopilot(session, lab, admin_id=users["admin"].id)
    return lab
