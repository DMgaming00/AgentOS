"""Sample Arena contenders: one scripted policy (mock scenario ``arena``), six configurations.

They differ only in configuration, so every score difference is attributable to a visible setting (see
``providers/arena_policy.py``). They are stand-ins for "the same model with different prompts and tools".
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.versions import initialize_agent_versioning
from agentos_core.db.models import AgentDefinition, Workspace

BASE = "You answer questions about technical documents using the tools you are given."
FORMAT = " Report prices as JSON numbers with no currency symbols. Cite the document section you used for every value."
CAREFUL = " Never guess: return null when the documents do not state the value."

CONTENDERS: list[dict[str, Any]] = [
    {
        "name": "Arena · Bare",
        "description": "Search only; no formatting, citation or abstention guidance.",
        "system": BASE,
        "tools": ["document_search"],
        "max_steps": 6,
    },
    {
        "name": "Arena · Formatter",
        "description": "Search; asks for JSON numbers and citations.",
        "system": BASE + FORMAT,
        "tools": ["document_search"],
        "max_steps": 6,
    },
    {
        "name": "Arena · Calculator",
        "description": "Formatter plus the calculator tool.",
        "system": BASE + FORMAT,
        "tools": ["document_search", "calculator"],
        "max_steps": 6,
    },
    {
        "name": "Arena · Careful",
        "description": "Calculator plus an instruction to abstain rather than guess.",
        "system": BASE + FORMAT + CAREFUL,
        "tools": ["document_search", "calculator"],
        "max_steps": 6,
    },
    {
        "name": "Arena · Careful (2 steps)",
        "description": "Careful, but limited to two steps.",
        "system": BASE + FORMAT + CAREFUL,
        "tools": ["document_search", "calculator"],
        "max_steps": 2,
    },
    {
        "name": "Arena · Ungrounded",
        "description": "Careful prompt but no document search: cannot ground any answer.",
        "system": BASE + FORMAT + CAREFUL,
        "tools": ["calculator"],
        "max_steps": 6,
    },
]


async def seed_contenders(
    session: AsyncSession, ws: Workspace, *, admin_id: uuid.UUID | None
) -> list[AgentDefinition]:
    existing = {
        a.name: a
        for a in (
            await session.execute(select(AgentDefinition).where(AgentDefinition.workspace_id == ws.id))
        ).scalars()
    }
    out: list[AgentDefinition] = []
    for spec in CONTENDERS:
        agent = existing.get(spec["name"])
        if agent is None:
            agent = AgentDefinition(
                id=uuid.uuid4(),
                workspace_id=ws.id,
                name=spec["name"],
                description=f"Agent Arena contender. {spec['description']}",
                system_instructions=spec["system"],
                provider="mock",
                model="mock-arena",
                model_configuration={"scenario": "arena"},
                allowed_tool_ids=spec["tools"],
                max_steps=spec["max_steps"],
                max_cost_usd=Decimal("0.5"),
                timeout_seconds=120,
            )
            session.add(agent)
            await session.flush()
            await initialize_agent_versioning(session, agent, created_by=admin_id)
        out.append(agent)
    settings = dict(ws.settings or {})
    ap = dict(settings.get("autopilot") or {})
    allowed = list(ap.get("allowed_models") or [])
    if {"provider": "mock", "model": "mock-arena"} not in allowed:
        allowed.append({"provider": "mock", "model": "mock-arena"})
        ap["allowed_models"] = allowed
        settings["autopilot"] = ap
        ws.settings = settings
    await session.flush()
    return out


async def seed_arena(session: AsyncSession, ws: Workspace, *, admin_id: uuid.UUID | None) -> None:
    """Opt-in seeding for a workspace: the sample contenders and the imported, hash-verified corpus."""
    from agentos_core.arena.corpus import import_corpus

    await seed_contenders(session, ws, admin_id=admin_id)
    await import_corpus(session, ws.id, created_by=admin_id)
