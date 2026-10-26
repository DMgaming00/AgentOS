"""Immutable agent configuration snapshots.

A snapshot contains every field that determines how an agent behaves. Two
snapshots with the same canonical JSON have the same hash and are the same
version (deduplicated per agent). Runs pin the version they started with.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agentos_core.util import canonical_json, sha256_hex

SNAPSHOT_SCHEMA_VERSION = 1


class AgentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    system_instructions: str = Field(default="", max_length=20_000)
    provider: str
    model: str = Field(min_length=1, max_length=200)
    model_configuration: dict[str, Any] = Field(default_factory=dict)
    allowed_tool_ids: list[str] = Field(default_factory=list)
    # Optional per-tool description overrides (only for allowed tools) and tool-use guidance.
    tool_descriptions: dict[str, str] = Field(default_factory=dict)
    tool_strategy: str = Field(default="", max_length=4000)
    max_steps: int = Field(ge=1, le=50)
    max_cost_usd: str
    timeout_seconds: int = Field(ge=1, le=3600)
    retry_max_attempts: int | None = Field(default=None, ge=1, le=10)

    @field_validator("allowed_tool_ids")
    @classmethod
    def _sorted(cls, v: list[str]) -> list[str]:
        return sorted(set(v))

    @field_validator("max_cost_usd", mode="before")
    @classmethod
    def _decimal(cls, v: object) -> str:
        return f"{Decimal(str(v)):.6f}"

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def config_hash(self) -> str:
        return config_hash(self.snapshot())


def config_hash(snapshot: dict[str, Any]) -> str:
    return sha256_hex(canonical_json(snapshot))


def config_from_agent(agent: Any) -> AgentConfig:
    """Build a snapshot from an ``AgentDefinition`` row (used for v1 backfill and manual edits)."""
    return AgentConfig(
        system_instructions=agent.system_instructions or "",
        provider=agent.provider,
        model=agent.model,
        model_configuration=dict(agent.model_configuration or {}),
        allowed_tool_ids=list(agent.allowed_tool_ids or []),
        max_steps=agent.max_steps,
        max_cost_usd=agent.max_cost_usd,
        timeout_seconds=agent.timeout_seconds,
    )


def config_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level diff; nested dicts are diffed one level deep (e.g. model_configuration.effort)."""
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if a == b:
            continue
        if isinstance(a, dict) and isinstance(b, dict):
            for sub in sorted(set(a) | set(b)):
                if a.get(sub) != b.get(sub):
                    changes.append({"field": f"{key}.{sub}", "before": a.get(sub), "after": b.get(sub)})
        elif isinstance(a, list) and isinstance(b, list):
            changes.append(
                {
                    "field": key,
                    "before": a,
                    "after": b,
                    "added": sorted(set(map(str, b)) - set(map(str, a))),
                    "removed": sorted(set(map(str, a)) - set(map(str, b))),
                }
            )
        else:
            changes.append({"field": key, "before": a, "after": b})
    return changes
