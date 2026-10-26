"""Agent config versioning and run pinning (Autopilot phase 1)."""

import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from agentos_core.autopilot.config import AgentConfig, config_diff, config_from_agent
from agentos_core.autopilot.versions import (
    apply_manual_edit,
    canary_bucket,
    ensure_version,
    lock_agent,
    resolve_pins,
)
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    AgentDeployment,
    StepAttempt,
    WorkflowStep,
)
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import add_agent, get_steps, running_workers, start_run, wait_run_status

pytestmark = pytest.mark.integration


async def versions_of(agent_id):
    async with unit_of_work() as s:
        return list(
            (
                await s.execute(
                    select(AgentConfigVersion)
                    .where(AgentConfigVersion.agent_id == agent_id)
                    .order_by(AgentConfigVersion.version)
                )
            ).scalars()
        )


async def edit(agent_id, **changes):
    async with unit_of_work() as s:
        agent = await lock_agent(s, agent_id)
        v = await apply_manual_edit(s, agent, changes, user_id=None)
        return v.version


async def test_seeded_agents_start_at_active_v1(seeded):
    for agent in seeded.agents.values():
        vs = await versions_of(agent.id)
        assert [v.version for v in vs] == [1] and vs[0].source == "initial"
        assert agent.active_version_id == vs[0].id
        assert vs[0].config_hash == config_from_agent(agent).config_hash()
    async with unit_of_work() as s:
        deps = (await s.execute(select(AgentDeployment))).scalars().all()
        n_agents = (await s.execute(select(func.count()).select_from(AgentDefinition))).scalar_one()
    # One initial deployment per agent in every workspace (the seed also creates the Autopilot Lab).
    assert {d.deployment_type for d in deps} == {"initial"} and len(deps) == n_agents
    assert {a.id for a in seeded.agents.values()} <= {d.agent_id for d in deps}


async def test_edits_create_content_addressed_versions(seeded):
    agent = seeded.agents["Processing Agent"]
    assert await edit(agent.id, system_instructions="new instructions") == 2
    assert (
        await edit(agent.id, system_instructions="new instructions") == 2
    )  # identical config -> same version
    assert await edit(agent.id, system_instructions=agent.system_instructions) == 1  # revert reuses v1
    vs = await versions_of(agent.id)
    assert [v.version for v in vs] == [1, 2] and vs[1].parent_version_id == vs[0].id
    async with unit_of_work() as s:
        row = await s.get(AgentDefinition, agent.id)
        deps = (
            (
                await s.execute(
                    select(AgentDeployment)
                    .where(AgentDeployment.agent_id == agent.id)
                    .order_by(AgentDeployment.deployed_at)
                )
            )
            .scalars()
            .all()
        )
    assert row.active_version_id == vs[0].id and row.system_instructions == agent.system_instructions
    assert [(d.deployment_type, d.status) for d in deps] == [
        ("initial", "superseded"),
        ("manual_edit", "superseded"),
        ("manual_edit", "active"),
    ]


async def test_running_and_queued_steps_keep_their_pinned_version(seeded):
    agent = await add_agent(seeded, "Pinned", scenario="echo", tools=[], config={"latency_ms": 300})
    v1 = (await versions_of(agent.id))[0]
    old_run = await start_run(seeded, {"nodes": [{"key": "a", "type": "agent_task", "agent": "Pinned"}]})
    # The agent is edited after the run was created but before any worker picks it up.
    await edit(agent.id, system_instructions="changed after the run started", max_steps=3)
    v2 = (await versions_of(agent.id))[1]
    new_run = await start_run(seeded, {"nodes": [{"key": "a", "type": "agent_task", "agent": "Pinned"}]})
    async with running_workers(1):
        await wait_run_status(old_run, {"succeeded"})
        await wait_run_status(new_run, {"succeeded"})
    old_step, new_step = (await get_steps(old_run))["a"], (await get_steps(new_run))["a"]
    assert old_step.agent_version_id == v1.id and new_step.agent_version_id == v2.id
    async with unit_of_work() as s:
        meta = {
            a.step_id: a.execution_metadata
            for a in (
                await s.execute(
                    select(StepAttempt).where(StepAttempt.step_id.in_([old_step.id, new_step.id]))
                )
            ).scalars()
        }
    assert meta[old_step.id]["agent_version"] == 1 and meta[new_step.id]["agent_version"] == 2


async def test_version_retry_policy_applies_to_new_steps(seeded):
    agent = await add_agent(seeded, "Retrying", scenario="echo", tools=[])
    await edit(agent.id, retry_max_attempts=5)
    run = await start_run(seeded, {"nodes": [{"key": "a", "type": "agent_task", "agent": "Retrying"}]})
    assert (await get_steps(run))["a"].max_attempts == 5


async def test_canary_bucketing_is_deterministic_and_proportional(seeded):
    agent = seeded.agents["Processing Agent"]
    assert canary_bucket(uuid.UUID(int=1), agent.id) == canary_bucket(uuid.UUID(int=1), agent.id)
    await edit(agent.id, system_instructions="candidate")
    vs = await versions_of(agent.id)
    async with unit_of_work() as s:
        await s.execute(
            update(AgentDefinition)
            .where(AgentDefinition.id == agent.id)
            .values(active_version_id=vs[0].id, canary_version_id=vs[1].id, canary_percent=25)
        )
    picks = []
    async with unit_of_work() as s:
        for i in range(400):
            picks.append((await resolve_pins(s, {agent.id}, uuid.UUID(int=i)))[agent.id])
        override = await resolve_pins(s, {agent.id}, uuid.UUID(int=1), {agent.id: vs[0].id})
    share = picks.count(vs[1].id) / len(picks)
    assert 0.18 < share < 0.32 and set(picks) == {vs[0].id, vs[1].id}
    assert override[agent.id] == vs[0].id


async def test_versions_are_immutable_in_the_database(seeded):
    v = (await versions_of(seeded.agents["Writer Agent"].id))[0]
    with pytest.raises(DBAPIError, match="immutable"):
        async with unit_of_work() as s:
            await s.execute(
                update(AgentConfigVersion).where(AgentConfigVersion.id == v.id).values(change_summary="x")
            )
    with pytest.raises(DBAPIError, match="not allowed"):
        async with unit_of_work() as s:
            await s.execute(AgentConfigVersion.__table__.delete().where(AgentConfigVersion.id == v.id))


async def test_unpinned_legacy_steps_still_run(seeded):
    """Runs created before migration 0002 have agent_version_id NULL and use the agent row."""
    run = await start_run(
        seeded, {"nodes": [{"key": "a", "type": "agent_task", "agent": "Processing Agent"}]}
    )
    async with unit_of_work() as s:
        await s.execute(update(WorkflowStep).where(WorkflowStep.run_id == run).values(agent_version_id=None))
    async with running_workers(1):
        r = await wait_run_status(run, {"succeeded", "failed"})
    assert r.status == "succeeded"


def test_config_validation_and_diff():
    base = AgentConfig(
        provider="mock",
        model="m",
        max_steps=4,
        max_cost_usd=1,
        timeout_seconds=30,
        allowed_tool_ids=["b", "a", "a"],
        model_configuration={"effort": "low"},
    )
    assert base.allowed_tool_ids == ["a", "b"] and base.max_cost_usd == "1.000000"
    other = base.model_copy(update={"model_configuration": {"effort": "high"}, "allowed_tool_ids": ["a"]})
    diff = {d["field"]: d for d in config_diff(base.snapshot(), other.snapshot())}
    assert diff["model_configuration.effort"]["after"] == "high" and diff["allowed_tool_ids"]["removed"] == [
        "b"
    ]
    with pytest.raises(ValueError):
        AgentConfig(provider="mock", model="m", max_steps=0, max_cost_usd=1, timeout_seconds=30)
    with pytest.raises(ValueError):
        AgentConfig(
            provider="mock",
            model="m",
            max_steps=1,
            max_cost_usd=1,
            timeout_seconds=30,
            approvals_enabled=False,
        )


async def test_ensure_version_numbers_are_sequential(seeded):
    agent = seeded.agents["Analysis Agent"]
    async with unit_of_work() as s:
        a = await lock_agent(s, agent.id)
        for i in range(3):
            cfg = config_from_agent(a).model_copy(update={"tool_strategy": f"s{i}"})
            await ensure_version(
                s,
                a,
                AgentConfig.model_validate(cfg.model_dump()),
                source="manual",
                parent_version_id=a.active_version_id,
                created_by=None,
            )
    assert [v.version for v in await versions_of(agent.id)] == [1, 2, 3, 4]
