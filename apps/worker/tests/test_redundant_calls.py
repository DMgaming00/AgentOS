"""Redundant tool calls: replayed (never executed twice), counted as evidence, and removable by a rule-generated change."""

import pytest
from sqlalchemy import func, select

from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.demo import FLAWED_INSTRUCTIONS
from agentos_core.autopilot.evaluation import create_evaluation_run
from agentos_core.autopilot.rules import generate_rule_candidates
from agentos_core.db.models import AgentConfigVersion, EvaluationRun, ToolExecution
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import add_agent, running_workers
from agentos_core.testing_autopilot import dataset, wait_row

pytestmark = pytest.mark.integration
FIXED = (
    FLAWED_INSTRUCTIONS
    + " Return every numeric value as a plain JSON number with no units or currency symbols."
)


async def evaluate(seeded, agent, split="development"):
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    async with unit_of_work() as s:
        v = await s.get(AgentConfigVersion, agent.active_version_id)
        ev = await create_evaluation_run(
            s,
            agent=agent,
            version=v,
            dataset=await dataset(ws, split),
            trials=1,
            created_by=dev,
            max_concurrency=6,
            per_case_max_cost_usd=0.25,
        )
    return await wait_row(EvaluationRun, ev.id, {"completed"}, 90)


@pytest.mark.timeout(240)
async def test_replays_are_counted_not_executed_and_a_rule_removes_them(seeded):
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    agent = await add_agent(
        seeded,
        "Redundant",
        scenario="redundant_extractor",
        tools=["document_search"],
        system_instructions=FIXED,
    )
    async with running_workers(2, concurrency=6):
        base = await evaluate(seeded, agent)
        assert (
            base.metrics["task_success_rate"]["value"] == 1.0
        )  # the answers are right; the habit is just wasteful
        async with unit_of_work() as s:
            executions = (
                await s.execute(
                    select(func.count(), func.sum(ToolExecution.replay_count)).where(
                        ToolExecution.tool_name == "document_search"
                    )
                )
            ).one()
        n_exec, replays = executions[0], int(executions[1] or 0)
        assert (
            n_exec >= 9 and replays == n_exec
        )  # each search ran once and was asked for twice: no duplicate execution rows

        async with unit_of_work() as s:
            (fix,) = await generate_rule_candidates(
                s, workspace_id=ws, agent_id=agent.id, diagnosis_id=None, user_id=dev
            )
        assert fix.rule_id == "redundant_tool_calls" and fix.diff[0]["field"] == "tool_strategy"
        async with unit_of_work() as s:
            cand = AgentConfig.model_validate(
                (await s.get(AgentConfigVersion, fix.candidate_version_id)).config_snapshot
            )
        assert "Never repeat a tool call" in cand.tool_strategy

        # the protected configuration stops asking twice: same task success, no replays, fewer input tokens
        improved = await evaluate_version(seeded, agent, fix.candidate_version_id)
        async with unit_of_work() as s:
            after = (await s.execute(select(func.sum(ToolExecution.replay_count)))).scalar_one()
        assert int(after or 0) == replays  # no new replays were added by the protected version
        assert improved.metrics["task_success_rate"]["value"] == 1.0
        assert improved.metrics["cost_per_successful_task_usd"] < base.metrics["cost_per_successful_task_usd"]


async def evaluate_version(seeded, agent, version_id):
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    async with unit_of_work() as s:
        v = await s.get(AgentConfigVersion, version_id)
        ev = await create_evaluation_run(
            s,
            agent=agent,
            version=v,
            dataset=await dataset(ws, "development"),
            trials=1,
            created_by=dev,
            max_concurrency=6,
            per_case_max_cost_usd=0.25,
        )
    return await wait_row(EvaluationRun, ev.id, {"completed"}, 90)
