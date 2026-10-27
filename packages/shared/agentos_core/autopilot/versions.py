"""Agent config versions: creation (content-addressed), activation, run pinning."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.config import AgentConfig, config_from_agent
from agentos_core.db.models import AgentConfigVersion, AgentDefinition, AgentDeployment, AgentEnvironmentPin
from agentos_core.util import sha256_hex, utcnow


async def lock_agent(session: AsyncSession, agent_id: uuid.UUID) -> AgentDefinition:
    return (
        await session.execute(
            select(AgentDefinition)
            .where(AgentDefinition.id == agent_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def ensure_version(
    session: AsyncSession,
    agent: AgentDefinition,
    config: AgentConfig,
    *,
    source: str,
    parent_version_id: uuid.UUID | None,
    created_by: uuid.UUID | None,
    summary: str = "",
) -> tuple[AgentConfigVersion, bool]:
    """Return the version with this exact config, creating it if needed. Returns (version, created).

    Callers must hold the agent row lock (``lock_agent``) so version numbers are gap-free.
    """
    digest = config.config_hash()
    existing = (
        await session.execute(
            select(AgentConfigVersion).where(
                AgentConfigVersion.agent_id == agent.id, AgentConfigVersion.config_hash == digest
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False
    latest = (
        await session.execute(
            select(func.max(AgentConfigVersion.version)).where(AgentConfigVersion.agent_id == agent.id)
        )
    ).scalar_one()
    version = AgentConfigVersion(
        id=uuid.uuid4(),
        workspace_id=agent.workspace_id,
        agent_id=agent.id,
        version=(latest or 0) + 1,
        parent_version_id=parent_version_id,
        config_snapshot=config.snapshot(),
        config_hash=digest,
        source=source,
        change_summary=summary[:2000],
        created_by=created_by,
    )
    session.add(version)
    await session.flush()
    return version, True


def mirror_to_agent(agent: AgentDefinition, config: AgentConfig) -> None:
    """Keep the legacy agent columns equal to the active version (existing API/UI read them)."""
    agent.system_instructions = config.system_instructions
    agent.provider = config.provider
    agent.model = config.model
    agent.model_configuration = dict(config.model_configuration)
    agent.allowed_tool_ids = list(config.allowed_tool_ids)
    agent.max_steps = config.max_steps
    agent.max_cost_usd = Decimal(config.max_cost_usd)
    agent.timeout_seconds = config.timeout_seconds


async def record_deployment(
    session: AsyncSession,
    agent: AgentDefinition,
    version: AgentConfigVersion,
    *,
    deployment_type: str,
    approved_by: uuid.UUID | None,
    reason: str,
    experiment_id: uuid.UUID | None = None,
    request_id: uuid.UUID | None = None,
    canary_percent: int = 100,
    supersede: bool = True,
) -> AgentDeployment:
    previous = agent.active_version_id
    if supersede:
        actives = (
            await session.execute(
                select(AgentDeployment).where(
                    AgentDeployment.agent_id == agent.id, AgentDeployment.status == "active"
                )
            )
        ).scalars()
        for d in actives:
            d.status = "superseded"
    dep = AgentDeployment(
        id=uuid.uuid4(),
        workspace_id=agent.workspace_id,
        agent_id=agent.id,
        config_version_id=version.id,
        previous_version_id=previous,
        deployment_type=deployment_type,
        canary_percent=canary_percent,
        experiment_id=experiment_id,
        request_id=request_id,
        approved_by=approved_by,
        reason=reason,
        status="active",
        deployed_at=utcnow(),
    )
    session.add(dep)
    await session.flush()
    return dep


async def initialize_agent_versioning(
    session: AsyncSession, agent: AgentDefinition, *, created_by: uuid.UUID | None
) -> AgentConfigVersion:
    """For a newly created agent: v1 from its fields, active, with an initial deployment record."""
    version, _ = await ensure_version(
        session,
        agent,
        config_from_agent(agent),
        source="initial",
        parent_version_id=None,
        created_by=created_by,
        summary="Initial version",
    )
    agent.active_version_id = version.id
    await record_deployment(
        session,
        agent,
        version,
        deployment_type="initial",
        approved_by=created_by,
        reason="Initial version",
        supersede=False,
    )
    return version


async def apply_manual_edit(
    session: AsyncSession, agent: AgentDefinition, changes: dict[str, Any], *, user_id: uuid.UUID
) -> AgentConfigVersion:
    """A direct edit by a developer creates (or reuses) a version and activates it immediately.

    This keeps the pre-Autopilot PATCH /agents contract. Optimizer-generated changes never take
    this path; they require an experiment and admin approval (see autopilot/deployment.py).
    """
    current = await active_config(session, agent)
    merged = current.model_copy(
        update={k: v for k, v in changes.items() if k in AgentConfig.model_fields}
    ).model_dump()
    config = AgentConfig.model_validate(merged)
    version, _ = await ensure_version(
        session,
        agent,
        config,
        source="manual",
        parent_version_id=agent.active_version_id,
        created_by=user_id,
        summary="Manual edit: " + ", ".join(sorted(changes)),
    )
    if version.id != agent.active_version_id:
        await record_deployment(
            session,
            agent,
            version,
            deployment_type="manual_edit",
            approved_by=user_id,
            reason="Direct edit by a workspace developer",
        )
        agent.active_version_id = version.id
        agent.canary_version_id = None
        agent.canary_percent = 0
    mirror_to_agent(agent, config)
    return version


async def active_config(session: AsyncSession, agent: AgentDefinition) -> AgentConfig:
    if agent.active_version_id is None:
        return config_from_agent(agent)
    version = await session.get(AgentConfigVersion, agent.active_version_id)
    assert version is not None
    return AgentConfig.model_validate(version.config_snapshot)


def canary_bucket(run_id: uuid.UUID, agent_id: uuid.UUID, seed: str | None = None) -> int:
    """Deterministic 0-99 bucket so a run always gets the same arm (reproducible, auditable).

    A release's seed (when set) is part of the hash, so a given run id routes the same way every time for that release
    and tests can choose seeds; without a seed the original hash is unchanged."""
    return int(sha256_hex(f"{seed}:{run_id}:{agent_id}" if seed else f"{run_id}:{agent_id}")[:8], 16) % 100


async def resolve_pins(
    session: AsyncSession,
    agent_ids: set[uuid.UUID],
    run_id: uuid.UUID,
    overrides: dict[uuid.UUID, uuid.UUID] | None = None,
    environment: str = "production",
) -> dict[uuid.UUID, uuid.UUID | None]:
    """Version each agent in a new run will use.

    Explicit override > (development/staging: that environment's pin, if the agent has one) > production canary bucket >
    active. Production is the agent's own pointers (the production-style local simulation, not a hosted deployment).
    """
    overrides = overrides or {}
    if not agent_ids:
        return {}
    rows = (await session.execute(select(AgentDefinition).where(AgentDefinition.id.in_(agent_ids)))).scalars()
    env_pins: dict[uuid.UUID, uuid.UUID] = {}
    if environment != "production":
        env_pins = dict(
            (
                await session.execute(
                    select(AgentEnvironmentPin.agent_id, AgentEnvironmentPin.version_id).where(
                        AgentEnvironmentPin.agent_id.in_(agent_ids),
                        AgentEnvironmentPin.environment == environment,
                    )
                )
            ).all()
        )
    pins: dict[uuid.UUID, uuid.UUID | None] = {}
    for agent in rows:
        if agent.id in overrides:
            pins[agent.id] = overrides[agent.id]
        elif agent.id in env_pins:
            pins[agent.id] = env_pins[agent.id]
        elif (
            agent.canary_version_id
            and agent.canary_percent
            and canary_bucket(run_id, agent.id, agent.canary_seed) < agent.canary_percent
        ):
            pins[agent.id] = agent.canary_version_id
        else:
            pins[agent.id] = agent.active_version_id
    return pins


@dataclass
class AgentSpec:
    """What the executor runs: an agent identity plus one immutable configuration."""

    id: uuid.UUID
    name: str
    version_id: uuid.UUID | None
    version: int | None
    system_instructions: str
    provider: str
    model: str
    model_configuration: dict[str, Any]
    allowed_tool_ids: list[str]
    max_steps: int
    max_cost_usd: Decimal
    timeout_seconds: int
    tool_descriptions: dict[str, str] = field(default_factory=dict)
    tool_strategy: str = ""


async def load_agent_spec(
    session: AsyncSession, agent_id: uuid.UUID, version_id: uuid.UUID | None
) -> AgentSpec:
    agent = (
        await session.execute(select(AgentDefinition).where(AgentDefinition.id == agent_id))
    ).scalar_one()
    if version_id is None:
        # Runs created before migration 0002 have no pin: they use the agent row, as they always did.
        cfg, version = config_from_agent(agent), None
    else:
        v = (
            await session.execute(select(AgentConfigVersion).where(AgentConfigVersion.id == version_id))
        ).scalar_one()
        if v.agent_id != agent_id:
            raise ValueError("pinned version belongs to a different agent")
        cfg, version = AgentConfig.model_validate(v.config_snapshot), v.version
    return AgentSpec(
        id=agent.id,
        name=agent.name,
        version_id=version_id,
        version=version,
        system_instructions=cfg.system_instructions,
        provider=cfg.provider,
        model=cfg.model,
        model_configuration=dict(cfg.model_configuration),
        allowed_tool_ids=list(cfg.allowed_tool_ids),
        max_steps=cfg.max_steps,
        max_cost_usd=Decimal(cfg.max_cost_usd),
        timeout_seconds=cfg.timeout_seconds,
        tool_descriptions=dict(cfg.tool_descriptions),
        tool_strategy=cfg.tool_strategy,
    )


async def version_retry_overrides(session: AsyncSession, version_ids: set[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not version_ids:
        return {}
    rows = await session.execute(
        select(AgentConfigVersion.id, AgentConfigVersion.config_snapshot).where(
            AgentConfigVersion.id.in_(version_ids)
        )
    )
    out = {}
    for vid, snap in rows.all():
        if snap.get("retry_max_attempts"):
            out[vid] = int(snap["retry_max_attempts"])
    return out
