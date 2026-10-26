"""Workflow validation and run creation scoped to a workspace."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.workflow_service import validate_for_workspace, workspace_agents, workspace_limits

__all__ = ["next_version", "validate_for_workspace", "workspace_agents", "workspace_limits"]


async def next_version(session: AsyncSession, workspace_id: uuid.UUID, name: str) -> int:
    from agentos_core.db.models import WorkflowDefinition

    current = (
        await session.execute(
            select(func.max(WorkflowDefinition.version)).where(
                WorkflowDefinition.workspace_id == workspace_id, WorkflowDefinition.name == name
            )
        )
    ).scalar_one_or_none()
    return (current or 0) + 1
