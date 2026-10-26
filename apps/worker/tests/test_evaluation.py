"""OpenEval integration: datasets, pinned evaluation runs, grading from real executions."""

import json

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

import openeval
from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.demo import DATASETS, EXTRACTION_AGENT
from agentos_core.autopilot.evaluation import (
    EvaluationError,
    create_evaluation_run,
    evaluation_report,
    import_dataset,
)
from agentos_core.autopilot.versions import ensure_version, lock_agent
from agentos_core.db.models import (
    AgentConfigVersion,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import running_workers, wait_for

pytestmark = pytest.mark.integration

FIX = " Return every numeric value as a plain JSON number with no units or currency symbols."


async def dataset(ws_id, split):
    async with unit_of_work() as s:
        return (
            await s.execute(
                select(EvaluationDataset).where(
                    EvaluationDataset.workspace_id == ws_id, EvaluationDataset.split == split
                )
            )
        ).scalar_one()


async def make_version(agent_id, **changes):
    async with unit_of_work() as s:
        agent = await lock_agent(s, agent_id)
        base = AgentConfig.model_validate(
            (await s.get(AgentConfigVersion, agent.active_version_id)).config_snapshot
        )
        cfg = AgentConfig.model_validate({**base.model_dump(), **changes})
        v, _ = await ensure_version(
            s,
            agent,
            cfg,
            source="manual_candidate",
            parent_version_id=agent.active_version_id,
            created_by=None,
        )
        return v


async def run_eval(seeded, version, split="validation", trials=1):
    agent = seeded.agents[EXTRACTION_AGENT]
    ds = await dataset(seeded.workspace.id, split)
    async with unit_of_work() as s:
        ev = await create_evaluation_run(
            s,
            agent=agent,
            version=version,
            dataset=ds,
            trials=trials,
            created_by=None,
            max_concurrency=4,
            per_case_max_cost_usd=0.25,
        )
        ev_id = ev.id

    async def done():
        async with unit_of_work() as s:
            e = await s.get(EvaluationRun, ev_id)
            return e if e.status == "completed" else None

    async with running_workers(2, concurrency=4):
        return await wait_for(done, timeout=90)


async def active_version(agent):
    async with unit_of_work() as s:
        return await s.get(AgentConfigVersion, agent.active_version_id)


async def test_seeded_datasets_are_split_and_hashed(seeded):
    counts = {}
    for split in ("development", "validation", "test"):
        ds = await dataset(seeded.workspace.id, split)
        counts[split] = ds.case_count
        assert ds.visibility == ("visible" if split == "development" else "hidden")
        assert (
            ds.dataset_hash
            == openeval.Dataset.model_validate(
                json.loads((DATASETS / f"numeric-extraction.v1.{split}.json").read_text())
            ).content_hash()
        )
    assert counts == {"development": 9, "validation": 16, "test": 8}


async def test_dataset_versions_are_immutable(seeded):
    raw = json.loads((DATASETS / "numeric-extraction.v1.validation.json").read_text())
    async with unit_of_work() as s:
        _, created = await import_dataset(
            s, seeded.workspace.id, openeval.Dataset.model_validate(raw), created_by=None
        )
    assert not created  # identical content is idempotent
    raw["cases"][0]["expected_output"] = {k: 0 for k in raw["cases"][0]["expected_output"]}
    with pytest.raises(EvaluationError, match="bump the version"):
        async with unit_of_work() as s:
            await import_dataset(
                s, seeded.workspace.id, openeval.Dataset.model_validate(raw), created_by=None
            )
    ds = await dataset(seeded.workspace.id, "validation")
    with pytest.raises(DBAPIError, match="immutable"):
        async with unit_of_work() as s:
            await s.execute(
                update(EvaluationDataset).where(EvaluationDataset.id == ds.id).values(case_count=1)
            )


async def test_flawed_agent_fails_currency_cases_with_traceable_errors(seeded):
    agent = seeded.agents[EXTRACTION_AGENT]
    ev = await run_eval(seeded, await active_version(agent))
    m = ev.metrics
    assert m["attempts"] == 16 and m["incomplete_executions"] == 0
    async with unit_of_work() as s:
        report = await evaluation_report(s, ev)
        steps = (
            (
                await s.execute(
                    select(WorkflowStep)
                    .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
                    .where(WorkflowRun.purpose == "evaluation")
                )
            )
            .scalars()
            .all()
        )
    by_case = {r["case_key"]: r for r in report["results"]}
    tags = {
        c["case_id"]: c["tags"]
        for c in json.loads((DATASETS / "numeric-extraction.v1.validation.json").read_text())["cases"]
    }
    for key, r in by_case.items():
        assert r["passed"] is ("currency" not in tags[key]), key
        if "currency" in tags[key]:
            assert (
                r["error"]["type"] == "invalid_model_output"
                and r["error"]["category"] == "OUTPUT_SCHEMA_INVALID"
            )
    expected_pass = sum("currency" not in t for t in tags.values())
    assert m["task_success_rate"] == {
        "value": expected_pass / 16,
        "numerator": expected_pass,
        "denominator": 16,
    }
    assert all(s.agent_version_id == ev.agent_version_id for s in steps)  # pinned to the version under test
    assert report["conditions"]["simulated"] is True and report["dataset"]["split"] == "validation"


async def test_prompt_fix_passes_and_skipping_search_fails_grounding(seeded):
    agent = seeded.agents[EXTRACTION_AGENT]
    base = await active_version(agent)
    fixed = await make_version(
        agent.id, system_instructions=base.config_snapshot["system_instructions"] + FIX
    )
    ev = await run_eval(seeded, fixed)
    assert ev.metrics["task_success_rate"]["value"] == 1.0
    assert ev.metrics["grounding_pass_rate"]["value"] == 1.0
    lazy = await make_version(
        agent.id,
        system_instructions=base.config_snapshot["system_instructions"] + FIX,
        allowed_tool_ids=[],
        max_steps=1,
    )
    ev2 = await run_eval(seeded, lazy)
    m2 = ev2.metrics
    assert m2["task_success_rate"]["value"] < 0.5
    # cheaper per execution, but it skipped required work:
    assert ev2.metrics["total_cost_usd"] < ev.metrics["total_cost_usd"]
    assert m2["tool_call_correctness"]["value"] == 0.0


async def test_expected_answers_never_reach_the_agent(seeded):
    agent = seeded.agents[EXTRACTION_AGENT]
    ev = await run_eval(seeded, await active_version(agent), split="test")
    async with unit_of_work() as s:
        steps = (
            (
                await s.execute(
                    select(WorkflowStep)
                    .join(EvaluationCaseResult, EvaluationCaseResult.workflow_run_id == WorkflowStep.run_id)
                    .where(EvaluationCaseResult.evaluation_run_id == ev.id)
                )
            )
            .scalars()
            .all()
        )
    for st in steps:
        cfg = json.dumps({"input": st.input, "node": st.node_config})
        assert "expected_output" not in cfg and "expected" not in st.input


async def test_budget_cap_marks_remaining_cases_not_run(seeded):
    agent = seeded.agents[EXTRACTION_AGENT]
    ds = await dataset(seeded.workspace.id, "validation")
    async with unit_of_work() as s:
        ev = await create_evaluation_run(
            s,
            agent=agent,
            version=await active_version(agent),
            dataset=ds,
            trials=1,
            created_by=None,
            max_concurrency=1,
            per_case_max_cost_usd=0.25,
        )
        ev.max_cost_usd = 0.003  # total cap far below 16 executions' cost
        ev_id = ev.id

    async def done():
        async with unit_of_work() as s:
            e = await s.get(EvaluationRun, ev_id)
            return e if e.status == "completed" else None

    async with running_workers(1):
        ev = await wait_for(done, timeout=60)
    assert ev.metrics["incomplete_executions"] > 0 and ev.metrics["attempts"] >= 1
    assert ev.metrics["attempts"] + ev.metrics["incomplete_executions"] == 16
    async with unit_of_work() as s:
        not_run = (
            (
                await s.execute(
                    select(EvaluationCaseResult).where(
                        EvaluationCaseResult.evaluation_run_id == ev_id, EvaluationCaseResult.passed.is_(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert all(r.error["type"] == "not_run" and r.workflow_run_id is None for r in not_run)
    with pytest.raises(DBAPIError, match="sealed"):
        async with unit_of_work() as s:
            await s.execute(update(EvaluationRun).where(EvaluationRun.id == ev_id).values(metrics={}))
