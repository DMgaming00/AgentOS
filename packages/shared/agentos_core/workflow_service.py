"""Workspace-scoped workflow validation and lookup, shared by the API and the engine (nested workflows)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.config import get_settings
from agentos_core.db.models import AgentDefinition, WorkflowDefinition, Workspace
from agentos_core.domain.dag import AgentInfo, Limits, ValidationResult, validate_definition
from agentos_core.tools.registry import get_registry


async def workspace_agents(session: AsyncSession, workspace_id: uuid.UUID) -> list[AgentDefinition]:
    return list(
        (
            await session.execute(select(AgentDefinition).where(AgentDefinition.workspace_id == workspace_id))
        ).scalars()
    )


async def workspace_limits(session: AsyncSession, workspace_id: uuid.UUID) -> Limits:
    s = get_settings()
    ws = await session.get(Workspace, workspace_id)
    ws_cap = (
        float((ws.settings or {}).get("max_run_cost_usd", s.max_run_cost_usd)) if ws else s.max_run_cost_usd
    )
    return Limits(max_nodes=s.max_dag_nodes, max_cost_usd=min(ws_cap, s.max_run_cost_usd))


async def validate_for_workspace(
    session: AsyncSession, workspace_id: uuid.UUID, definition: dict[str, Any]
) -> tuple[ValidationResult, dict[str, Any]]:
    from agentos_core.engine.core import AgentRef

    agents = await workspace_agents(session, workspace_id)
    info = {a.name: AgentInfo(a.name, frozenset(a.allowed_tool_ids or [])) for a in agents}
    result = validate_definition(
        definition,
        agents=info,
        tools=get_registry().infos(),
        limits=await workspace_limits(session, workspace_id),
    )
    return result, {a.name: AgentRef(a.id, a.name) for a in agents}


async def latest_workflow(
    session: AsyncSession, workspace_id: uuid.UUID, name: str
) -> WorkflowDefinition | None:
    return (
        await session.execute(
            select(WorkflowDefinition)
            .where(WorkflowDefinition.workspace_id == workspace_id, WorkflowDefinition.name == name)
            .order_by(WorkflowDefinition.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
