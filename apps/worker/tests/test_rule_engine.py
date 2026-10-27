"""Rule-generated candidates from real traces: evidence is real, policy still decides, the experiment still judges."""

import json
import uuid

import pytest
from sqlalchemy import select

from agentos_core.autopilot.demo import EXTRACTION_AGENT
from agentos_core.autopilot.evaluation import create_evaluation_run
from agentos_core.autopilot.experiments import start_experiment
from agentos_core.autopilot.rules import RULES, catalog, collect_evidence, generate_rule_candidates
from agentos_core.db.models import (
    AgentConfigVersion,
    EvaluationDataset,
    EvaluationRun,
    OptimizationExperiment,
    WorkflowStep,
)
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import running_workers, wait_for

pytestmark = pytest.mark.integration


async def dataset(ws, split):
    async with unit_of_work() as s:
        return (
            await s.execute(
                select(EvaluationDataset).where(
                    EvaluationDataset.workspace_id == ws, EvaluationDataset.split == split
                )
            )
        ).scalar_one()


async def wait_status(model, id_, done_states, timeout=120):
    async def check():
        async with unit_of_work() as s:
            row = await s.get(model, id_)
            return row if row.status in done_states else None

    return await wait_for(check, timeout=timeout)


@pytest.mark.timeout(400)
async def test_rules_read_real_traces_and_the_candidate_wins_its_experiment(seeded):
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    agent = seeded.agents[EXTRACTION_AGENT]

    # With no traces no rule has any evidence: nothing is proposed.
    async with unit_of_work() as s:
        assert (
            await generate_rule_candidates(
                s, workspace_id=ws, agent_id=agent.id, diagnosis_id=None, user_id=dev
            )
            == []
        )

    async with running_workers(2, concurrency=6):
        async with unit_of_work() as s:
            v1 = await s.get(AgentConfigVersion, agent.active_version_id)
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
        await wait_status(EvaluationRun, ev.id, {"completed"}, 90)

        async with unit_of_work() as s:
            evidence = await collect_evidence(s, ws, agent.id)
            failed = [x for x in evidence.steps if x.status == "failed"]
        assert failed and all("is not of type" in x.error_message for x in failed)

        async with unit_of_work() as s:
            cands = await generate_rule_candidates(
                s, workspace_id=ws, agent_id=agent.id, diagnosis_id=None, user_id=dev
            )
        assert [c.rule_id for c in cands] == ["numeric_as_string"]
        fix = cands[0]
        assert (
            fix.generator == "rule_engine"
            and fix.rule_version == "1"
            and fix.candidate_type == "prompt_revision"
        )
        assert fix.status == "proposed" and fix.simulated is False and fix.projected is False
        assert any(d["field"] == "system_instructions" for d in fix.diff)
        # every cited piece of evidence is a real failed step of this agent
        async with unit_of_work() as s:
            cited = {e["id"] for e in fix.evidence}
            rows = (
                (
                    await s.execute(
                        select(WorkflowStep).where(WorkflowStep.id.in_([uuid.UUID(i) for i in cited]))
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == len(cited) > 0 and all(
                r.status == "failed" and r.agent_definition_id == agent.id for r in rows
            )
            # the same proposal is not duplicated while it is open
            assert (
                await generate_rule_candidates(
                    s, workspace_id=ws, agent_id=agent.id, diagnosis_id=None, user_id=dev
                )
                == []
            )

        # The experiment, not the rule, decides: a paired comparison on held-out cases.
        val = await dataset(ws, "validation")
        async with unit_of_work() as s:
            exp = await start_experiment(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                candidate_id=fix.id,
                dataset_id=val.id,
                trials=1,
                user_id=dev,
            )
        exp = await wait_status(OptimizationExperiment, exp.id, {"completed", "failed"}, 150)
    assert exp.decision == "eligible" and exp.eligibility["improvement_demonstrated"]
    assert exp.results["comparison"]["n_matched"] >= 10 and exp.results["comparison"]["ci95"][0] > 0


def test_catalog_is_versioned_and_served_verbatim():
    cat = catalog()
    assert cat["version"] == "rules-v1" and {r["id"] for r in cat["rules"]} == set(RULES)
    json.dumps(cat)
