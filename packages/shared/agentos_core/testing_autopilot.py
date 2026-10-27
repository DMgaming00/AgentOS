"""Shared test support: drive the real Autopilot pipeline to an eligible experiment, and record a clean security run."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from agentos_core.autopilot.demo import EXTRACTION_AGENT
from agentos_core.autopilot.evaluation import create_evaluation_run
from agentos_core.autopilot.experiments import start_experiment
from agentos_core.autopilot.rules import generate_rule_candidates
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    EvaluationDataset,
    EvaluationRun,
    OptimizationExperiment,
    SecurityEvaluationRun,
)
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import Seeded, running_workers, wait_for
from agentos_core.util import utcnow


async def dataset(ws: uuid.UUID, split: str) -> EvaluationDataset:
    async with unit_of_work() as s:
        return (
            await s.execute(
                select(EvaluationDataset).where(
                    EvaluationDataset.workspace_id == ws,
                    EvaluationDataset.split == split,
                    EvaluationDataset.name == "numeric-extraction",
                )
            )
        ).scalar_one()


async def wait_row(model: Any, id_: uuid.UUID, states: set[str], timeout: float = 150) -> Any:
    async def check() -> Any:
        async with unit_of_work() as s:
            row = await s.get(model, id_)
            return row if row is not None and row.status in states else None

    return await wait_for(check, timeout=timeout)


async def eligible_experiment(seeded: Seeded) -> tuple[AgentDefinition, OptimizationExperiment]:
    """The real pipeline: a failing baseline, rule-generated fix, paired experiment on held-out cases."""
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    agent = seeded.agents[EXTRACTION_AGENT]
    async with running_workers(2, concurrency=6):
        async with unit_of_work() as s:
            v1 = await s.get(AgentConfigVersion, agent.active_version_id)
            assert v1 is not None
            ev = await create_evaluation_run(
                s,
                agent=agent,
                version=v1,
                dataset=await dataset(ws, "development"),
                trials=1,
                created_by=dev,
                max_concurrency=6,
                per_case_max_cost_usd=0.25,
            )
        await wait_row(EvaluationRun, ev.id, {"completed"}, 90)
        async with unit_of_work() as s:
            (fix,) = await generate_rule_candidates(
                s, workspace_id=ws, agent_id=agent.id, diagnosis_id=None, user_id=dev
            )
        async with unit_of_work() as s:
            exp = await start_experiment(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                candidate_id=fix.id,
                dataset_id=(await dataset(ws, "validation")).id,
                trials=1,
                user_id=dev,
            )
        exp = await wait_row(OptimizationExperiment, exp.id, {"completed", "failed"}, 150)
    assert exp.decision == "eligible"
    return agent, exp


async def clean_security_run(ws: uuid.UUID) -> None:
    """A completed Security Center evaluation record with a clean result (the suite itself is tested in test_security_center)."""
    async with unit_of_work() as s:
        s.add(
            SecurityEvaluationRun(
                id=uuid.uuid4(),
                workspace_id=ws,
                suite_version="test",
                status="completed",
                phase="done",
                completed_at=utcnow(),
                results={
                    "metrics": {"unsafe_side_effects": 0, "successful_policy_enforcement": {"value": 1.0}},
                    "counts": {"policy": {"total": 11, "pass": 11, "fail": 0, "error": 0}},
                },
            )
        )
