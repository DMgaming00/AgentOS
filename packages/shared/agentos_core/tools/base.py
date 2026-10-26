"""Tool interface. Every tool declares its schema, side effects and approval policy."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from pydantic import BaseModel

from agentos_core.storage import Storage


class SideEffect(StrEnum):
    NONE = "none"  # pure / read-only
    INTERNAL = "internal"  # writes AgentOS-controlled state (artifact storage)
    EXTERNAL = "external"  # affects systems outside AgentOS


@dataclass
class ToolContext:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    step_id: uuid.UUID
    node_key: str
    attempt_id: uuid.UUID
    lease_token: uuid.UUID
    tool_execution_id: uuid.UUID
    idempotency_key: str
    storage: Storage


class Tool:
    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]
    result_model: ClassVar[type[BaseModel] | None] = None
    permission: ClassVar[str] = "read"
    side_effect: ClassVar[SideEffect] = SideEffect.NONE
    timeout_seconds: ClassVar[float] = 30.0
    idempotent: ClassVar[bool] = True
    requires_approval: ClassVar[bool] = False
    enabled_by_default: ClassVar[bool] = True

    async def run(self, args: BaseModel, ctx: ToolContext) -> dict[str, Any]:
        raise NotImplementedError

    def describe_action(self, args: BaseModel) -> tuple[str, str | None]:
        """Human-readable (action, affected_resource) for approval screens."""
        return f"{self.name}", None

    @classmethod
    def input_schema(cls) -> dict[str, Any]:
        schema = cls.args_model.model_json_schema()
        schema.pop("title", None)
        return schema

    @classmethod
    def metadata(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "description": cls.description,
            "input_schema": cls.input_schema(),
            "result_schema": cls.result_model.model_json_schema() if cls.result_model else None,
            "permission": cls.permission,
            "side_effect": cls.side_effect.value,
            "timeout_seconds": cls.timeout_seconds,
            "idempotent": cls.idempotent,
            "requires_approval": cls.requires_approval,
            "enabled": cls.enabled_by_default,
        }
