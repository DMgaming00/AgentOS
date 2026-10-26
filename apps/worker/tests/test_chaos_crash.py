"""Worker-crash faults with real worker processes: the lease expires and another worker finishes the work."""

import asyncio

import pytest
from sqlalchemy import select

from agentos_core.chaos.experiments import create_experiment
from agentos_core.chaos.faults import ChaosPlan
from agentos_core.db.models import ChaosExperiment, StepAttempt, WorkflowStep
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import save_workflow, spawn_worker, stop_workers, wait_for

pytestmark = pytest.mark.integration
FAST = {"max_attempts": 3, "backoff_base_seconds": 0.1, "backoff_max_seconds": 0.2}


async def test_worker_crash_faults_are_recovered_through_lease_expiry(seeded):
    wf = {
        "nodes": [
            {
                "key": "r1",
                "type": "agent_task",
                "agent": "Research Agent",
                "tools": ["document_search"],
                "input": {"platform": "Nimbus Inference"},
                "retry": FAST,
            },
            {"key": "agg", "type": "aggregation", "depends_on": ["r1"]},
        ]
    }
    plan = ChaosPlan.model_validate(
        {
            "faults": [{"type": "worker_crash", "point": "before_model_call", "probability": 1.0}],
            "trials": 3,
            "seed": 21,
            "duration_seconds": 150,
        }
    )
    wf_id = await save_workflow(seeded, "crash-wf", wf)
    async with unit_of_work() as s:
        exp_id = (
            await create_experiment(
                s, workspace_id=seeded.workspace.id, user_id=None, name="crash", workflow_id=wf_id, plan=plan
            )
        ).id
    procs = [spawn_worker("chaos-a", 2), spawn_worker("chaos-b", 2)]
    respawns = 0
    try:

        async def done():
            nonlocal respawns
            for i, p in enumerate(procs):  # a supervisor: dead workers are replaced, as in production
                if p.poll() is not None:
                    respawns += 1
                    procs[i] = spawn_worker(f"chaos-r{respawns}", 2)
            async with unit_of_work() as s:
                e = await s.get(ChaosExperiment, exp_id)
                return e if e.status != "running" else None

        exp = await wait_for(done, timeout=120, interval=0.5)
    finally:
        stop_workers(*procs)
    assert exp.status == "completed", exp.abort_reason
    m = exp.results["metrics"]
    assert respawns >= 1  # workers really died (exit 137), they were not simulated away
    assert m["task_completion_rate"] == {"value": 1.0, "numerator": 3, "denominator": 3}
    assert m["recovery_rate"]["value"] == 1.0 and exp.results["injections_by_type"] == {"worker_crash": 3}
    assert (
        m["time_to_recovery_ms"]["n"] == 3 and m["time_to_recovery_ms"]["median"] >= 1500
    )  # waited for the 2 s lease
    assert m["state_consistency"]["value"] == 1.0
    async with unit_of_work() as s:
        attempts = (
            (
                await s.execute(
                    select(StepAttempt)
                    .join(WorkflowStep, WorkflowStep.id == StepAttempt.step_id)
                    .where(WorkflowStep.node_key == "r1")
                )
            )
            .scalars()
            .all()
        )
    expired = [a for a in attempts if a.status == "lease_expired"]
    finished = [a for a in attempts if a.status == "succeeded"]
    # Every crashed attempt was abandoned with its worker; each of the three steps still finished. (A worker that
    # dies takes every step it was running with it, so some steps need more than two attempts.)
    assert len(expired) >= 3 and len(finished) == 3 and all(a.attempt_number >= 2 for a in finished)
    _ = asyncio
