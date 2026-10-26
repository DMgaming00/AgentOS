"""Test helpers shared by core, API and worker test suites."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from agentos_core.autopilot.versions import initialize_agent_versioning
from agentos_core.db.models import (
    AgentDefinition,
    ApprovalRequest,
    User,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
    Workspace,
)
from agentos_core.db.session import get_sessionmaker
from agentos_core.demo import seed
from agentos_core.domain.dag import WorkflowSpec
from agentos_core.engine.core import AgentRef, materialize_run
from agentos_core.engine.tx import unit_of_work


@dataclass
class Seeded:
    workspace: Workspace
    users: dict[str, User]
    agents: dict[str, AgentDefinition]


async def seed_demo() -> Seeded:
    async with unit_of_work() as session:
        ws = await seed(session, provider="mock")
    async with get_sessionmaker()() as session:
        users = {u.role: u for u in (await session.execute(select(User))).scalars()}
        agents = {
            a.name: a
            for a in (
                await session.execute(select(AgentDefinition).where(AgentDefinition.workspace_id == ws.id))
            ).scalars()
        }
    return Seeded(workspace=ws, users=users, agents=agents)


async def add_agent(
    seeded: Seeded, name: str, *, scenario: str, tools: list[str], **kw: Any
) -> AgentDefinition:
    async with unit_of_work() as session:
        agent = AgentDefinition(
            id=uuid.uuid4(),
            workspace_id=seeded.workspace.id,
            name=name,
            description=name,
            system_instructions=kw.pop("system_instructions", "test"),
            provider=kw.pop("provider", "mock"),
            model=kw.pop("model", f"mock-{scenario}"),
            model_configuration={"scenario": scenario, **kw.pop("config", {})},
            allowed_tool_ids=tools,
            max_steps=kw.pop("max_steps", 8),
            timeout_seconds=kw.pop("timeout_seconds", 60),
            **kw,
        )
        session.add(agent)
        await session.flush()
        await initialize_agent_versioning(session, agent, created_by=None)
    seeded.agents[name] = agent
    return agent


async def start_run(
    seeded: Seeded,
    definition: dict[str, Any],
    *,
    name: str | None = None,
    run_input: dict[str, Any] | None = None,
) -> uuid.UUID:
    spec = WorkflowSpec.model_validate(definition)
    async with unit_of_work() as session:
        wf = WorkflowDefinition(
            id=uuid.uuid4(),
            workspace_id=seeded.workspace.id,
            name=f"{name or 'wf'}-{uuid.uuid4().hex[:8]}",
            version=1,
            definition=definition,
        )
        session.add(wf)
        await session.flush()
        run = await materialize_run(
            session,
            workflow=wf,
            spec=spec,
            run_input=run_input or {},
            requested_by=seeded.users["admin"].id,
            agents_by_name={n: AgentRef(a.id, n) for n, a in seeded.agents.items()},
        )
        return run.id


async def get_run(run_id: uuid.UUID) -> WorkflowRun:
    async with get_sessionmaker()() as session:
        return (await session.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))).scalar_one()


async def get_steps(run_id: uuid.UUID) -> dict[str, WorkflowStep]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_id))).scalars()
        return {s.node_key: s for s in rows}


async def pending_approvals(run_id: uuid.UUID) -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list(
            (
                await session.execute(
                    select(ApprovalRequest)
                    .where(ApprovalRequest.run_id == run_id, ApprovalRequest.status == "pending")
                    .order_by(ApprovalRequest.requested_at)
                )
            ).scalars()
        )


async def wait_for(predicate: Any, timeout: float = 30.0, interval: float = 0.1) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        value = await predicate()
        if value:
            return value
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError("condition not met in time")
        await asyncio.sleep(interval)


async def wait_run_status(run_id: uuid.UUID, statuses: set[str], timeout: float = 30.0) -> WorkflowRun:
    async def check() -> WorkflowRun | None:
        run = await get_run(run_id)
        return run if run.status in statuses else None

    result: WorkflowRun = await wait_for(check, timeout)
    return result


@contextlib.asynccontextmanager
async def running_workers(n: int = 1, **kw: Any):  # type: ignore[no-untyped-def]
    """Run ``n`` in-process workers for the duration of the block."""
    from agentos_worker.scheduler import Worker

    workers = [Worker(f"test-worker-{i}-{uuid.uuid4().hex[:4]}", **kw) for i in range(n)]
    tasks = [asyncio.create_task(w.run()) for w in workers]
    try:
        yield workers
    finally:
        for w in workers:
            w.stop()
        await asyncio.gather(*tasks, return_exceptions=True)
