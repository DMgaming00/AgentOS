"""End-to-end Autopilot loop: run -> detect -> diagnose -> propose -> experiment -> approve -> deploy
-> monitor -> rollback. Real PostgreSQL, real workers, mock (simulated) models."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from agentos_core.autopilot import deployment as dep
from agentos_core.autopilot.demo import EXTRACTION_AGENT, live_extraction_workflow
from agentos_core.autopilot.diagnosis import diagnose
from agentos_core.autopilot.evaluation import create_evaluation_run
from agentos_core.autopilot.experiments import start_experiment
from agentos_core.autopilot.failures import analyze, clusters
from agentos_core.autopilot.optimizer import (
    ProposedCandidate,
    ProposedChange,
    generate_candidates,
    propose_manual_candidate,
)
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    AgentDeployment,
    DeploymentRequest,
    EvaluationDataset,
    EvaluationRun,
    OptimizationExperiment,
    RolloutEvent,
)
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import get_steps, running_workers, start_run, wait_for, wait_run_status
from agentos_core.util import utcnow

pytestmark = pytest.mark.integration


async def ds(ws_id, split):
    async with unit_of_work() as s:
        return (
            await s.execute(
                select(EvaluationDataset).where(
                    EvaluationDataset.workspace_id == ws_id, EvaluationDataset.split == split
                )
            )
        ).scalar_one()


async def agent_row(agent_id):
    async with unit_of_work() as s:
        return await s.get(AgentDefinition, agent_id)


async def wait_experiment(exp_id, timeout=120):
    async def done():
        async with unit_of_work() as s:
            e = await s.get(OptimizationExperiment, exp_id)
            return e if e.status != "running" else None

    return await wait_for(done, timeout=timeout)


async def wait_eval(ev_id):
    async def done():
        async with unit_of_work() as s:
            e = await s.get(EvaluationRun, ev_id)
            return e if e.status == "completed" else None

    return await wait_for(done, timeout=90)


@pytest.mark.timeout(400)
async def test_full_optimization_cycle_with_fake_improvement_and_rollback(seeded):
    ws, admin, dev = seeded.workspace.id, seeded.users["admin"].id, seeded.users["developer"].id
    agent = seeded.agents[EXTRACTION_AGENT]
    v1_id = agent.active_version_id
    async with running_workers(2, concurrency=6):
        # 1. The flawed agent executes the development benchmark; some cases fail.
        async with unit_of_work() as s:
            v1 = await s.get(AgentConfigVersion, v1_id)
            ev = await create_evaluation_run(
                s,
                agent=agent,
                version=v1,
                dataset=await ds(ws, "development"),
                trials=1,
                created_by=dev,
                max_concurrency=6,
                per_case_max_cost_usd=0.25,
            )
        dev_eval = await wait_eval(ev.id)
        assert 0 < dev_eval.metrics["task_success_rate"]["value"] < 1

        # 2. Failure Intelligence detects and clusters the failures from traces.
        async with unit_of_work() as s:
            summary = await analyze(s, ws, window_hours=1)
            again = await analyze(s, ws, window_hours=1)
            groups = await clusters(s, ws, since=utcnow() - timedelta(hours=1))
        assert summary["by_category"]["OUTPUT_SCHEMA_INVALID"] >= 1 and again["new"] == 0  # idempotent
        schema = next(g for g in groups if g["category"] == "OUTPUT_SCHEMA_INVALID")
        assert schema["agent_id"] == str(agent.id) and not schema["is_infrastructure"]

        # 3. Diagnosis: evidence-linked hypothesis, labeled as such.
        async with unit_of_work() as s:
            d = await diagnose(
                s, workspace_id=ws, agent_id=agent.id, category="OUTPUT_SCHEMA_INVALID", user_id=dev
            )
        assert d.status == "hypothesis" and "currency" in d.hypothesis and d.simulated
        assert all(e.split(":")[0] in ("signal", "event", "step", "tool_execution") for e in d.evidence_refs)
        assert any("not a verified root cause" in lim for lim in d.limitations)

        # 4. Optimizer proposes candidates; out-of-policy ones are rejected in code.
        async with unit_of_work() as s:
            cands = await generate_candidates(
                s, workspace_id=ws, agent_id=agent.id, diagnosis_id=d.id, user_id=dev
            )
        by_title = {c.title: c for c in cands}
        fix, fast, escalate = (
            by_title["Return numeric values as JSON numbers"],
            by_title["Fast path: answer without retrieval"],
            by_title["Persist extractions as artifacts"],
        )
        assert fix.status == fast.status == "proposed" and escalate.status == "rejected_policy"
        assert escalate.candidate_version_id is None and "adds tools" in escalate.policy_violations[0]
        assert any(c["field"] == "system_instructions" for c in fix.diff)

        # 5. Controlled experiments on the held-out validation split.
        val = await ds(ws, "validation")
        async with unit_of_work() as s:
            e_fix = await start_experiment(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                candidate_id=fix.id,
                dataset_id=val.id,
                trials=1,
                user_id=dev,
            )
            e_fast = await start_experiment(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                candidate_id=fast.id,
                dataset_id=val.id,
                trials=1,
                user_id=dev,
            )
        e_fix, e_fast = await wait_experiment(e_fix.id), await wait_experiment(e_fast.id)
        assert e_fix.decision == "eligible" and e_fix.eligibility["improvement_demonstrated"]
        assert e_fix.results["candidate"]["metrics"]["task_success_rate"]["value"] == 1.0
        # Demo 2: cheaper per execution, but it skips required work -> rejected.
        assert e_fast.decision == "not_eligible"
        assert e_fast.results["cost"]["candidate_total"] < e_fast.results["cost"]["baseline_total"]
        assert e_fast.results["comparison"]["regressions"]
        with pytest.raises(dep.DeploymentError, match="only eligible"):
            async with unit_of_work() as s:
                await dep.request_promotion(s, workspace_id=ws, experiment_id=e_fast.id, user_id=dev)

        # 6. Human approval is required; the request is idempotent and single-use.
        async with unit_of_work() as s:
            req = await dep.request_promotion(
                s, workspace_id=ws, experiment_id=e_fix.id, user_id=dev, reason="fix"
            )
            same = await dep.request_promotion(s, workspace_id=ws, experiment_id=e_fix.id, user_id=dev)
        assert same.id == req.id and (await agent_row(agent.id)).active_version_id == v1_id
        old_run = await start_run(seeded, live_extraction_workflow(), name="pre-promotion")
        async with unit_of_work() as s:
            await dep.decide_request(
                s, workspace_id=ws, request_id=req.id, approve=True, user_id=admin, reason="ok"
            )
        with pytest.raises(dep.DeploymentError, match="already approved"):
            async with unit_of_work() as s:
                await dep.decide_request(
                    s, workspace_id=ws, request_id=req.id, approve=True, user_id=admin, reason="x"
                )
        v2_id = (await agent_row(agent.id)).active_version_id
        assert v2_id == fix.candidate_version_id
        assert (await get_steps(old_run))["extract"].agent_version_id == v1_id  # existing run keeps its pin

        # Live traffic on v2 establishes a healthy baseline for monitoring.
        live_v2 = [await start_run(seeded, live_extraction_workflow(), name="live-v2") for _ in range(5)]
        for r in live_v2:
            assert (await wait_run_status(r, {"succeeded", "failed"})).status == "succeeded"

        # 7. Demo 3: a step-limit "cost cap" that passes offline validation but breaks live two-subject tasks.
        async with unit_of_work() as s:
            cap = await propose_manual_candidate(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                user_id=dev,
                proposal=ProposedCandidate(
                    title="Cap agent steps at 2",
                    changes=[ProposedChange(field="max_steps", value=2, reason="cost cap")],
                ),
            )
            e_cap = await start_experiment(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                candidate_id=cap.id,
                dataset_id=val.id,
                trials=1,
                user_id=dev,
            )
        e_cap = await wait_experiment(e_cap.id)
        assert e_cap.decision == "eligible" and not e_cap.eligibility["improvement_demonstrated"]
        async with unit_of_work() as s:
            req2 = await dep.request_promotion(s, workspace_id=ws, experiment_id=e_cap.id, user_id=dev)
            await dep.decide_request(
                s, workspace_id=ws, request_id=req2.id, approve=True, user_id=admin, reason="ok"
            )
        v3_id = (await agent_row(agent.id)).active_version_id
        live_v3 = [await start_run(seeded, live_extraction_workflow(), name="live-v3") for _ in range(5)]
        for r in live_v3:
            assert (await wait_run_status(r, {"succeeded", "failed"})).status == "failed"
            assert (await get_steps(r))["extract"].error["type"] == "step_limit_exceeded"

        async def flagged():
            async with unit_of_work() as s:
                return (
                    (
                        await s.execute(
                            select(RolloutEvent).where(RolloutEvent.event_type == "regression_detected")
                        )
                    )
                    .scalars()
                    .first()
                )

        alert = await wait_for(flagged, timeout=20)
        assert (
            alert.payload["current"]["failure_rate"] == 1.0
            and alert.payload["previous"]["failure_rate"] == 0.0
        )

        # 8. Rollback restores v2 for new runs; runs made on v3 stay pinned to v3.
        async with unit_of_work() as s:
            rb = await dep.rollback(
                s, workspace_id=ws, agent_id=agent.id, user_id=admin, reason="live regression"
            )
        assert rb.config_version_id == v2_id and (await agent_row(agent.id)).active_version_id == v2_id
        after = await start_run(seeded, live_extraction_workflow(), name="after-rollback")
        assert (await wait_run_status(after, {"succeeded", "failed"})).status == "succeeded"
        assert (await get_steps(after))["extract"].agent_version_id == v2_id
        for r in live_v3:
            assert (await get_steps(r))["extract"].agent_version_id == v3_id

    async with unit_of_work() as s:
        history = (
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
        unsuccessful = (
            await s.execute(
                select(func.count())
                .select_from(OptimizationExperiment)
                .where(OptimizationExperiment.decision == "not_eligible")
            )
        ).scalar_one()
    assert [h.deployment_type for h in history] == ["initial", "promotion", "promotion", "rollback"]
    assert [h.status for h in history] == ["superseded", "superseded", "rolled_back", "active"]
    assert unsuccessful == 1  # rejected experiments are preserved


async def test_expired_and_stale_promotion_requests(seeded):
    ws, admin, dev = seeded.workspace.id, seeded.users["admin"].id, seeded.users["developer"].id
    agent = seeded.agents[EXTRACTION_AGENT]
    async with unit_of_work() as s:
        cand = await propose_manual_candidate(
            s,
            workspace_id=ws,
            agent_id=agent.id,
            user_id=dev,
            proposal=ProposedCandidate(
                title="numbers",
                changes=[
                    ProposedChange(
                        field="system_instructions",
                        value=agent.system_instructions
                        + " Return every numeric value as a plain JSON number.",
                    )
                ],
            ),
        )
        exp = await start_experiment(
            s,
            workspace_id=ws,
            agent_id=agent.id,
            candidate_id=cand.id,
            dataset_id=(await ds(ws, "validation")).id,
            trials=1,
            user_id=dev,
        )
    async with running_workers(2, concurrency=6):
        exp = await wait_experiment(exp.id)
    assert exp.decision == "eligible"
    async with unit_of_work() as s:
        req = await dep.request_promotion(s, workspace_id=ws, experiment_id=exp.id, user_id=dev)
        await s.execute(
            update(DeploymentRequest)
            .where(DeploymentRequest.id == req.id)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
    async with unit_of_work() as s:
        decided = await dep.decide_request(
            s, workspace_id=ws, request_id=req.id, approve=True, user_id=admin, reason=None
        )
    assert decided.status == "expired"
    async with unit_of_work() as s:
        assert (await s.get(DeploymentRequest, req.id)).status == "expired"  # persisted
        assert (
            await s.get(AgentDefinition, agent.id)
        ).active_version_id == agent.active_version_id  # not promoted
    with pytest.raises(dep.DeploymentError, match="already expired"):
        async with unit_of_work() as s:
            await dep.decide_request(
                s, workspace_id=ws, request_id=req.id, approve=True, user_id=admin, reason=None
            )
    # The active version changes (manual edit) -> the experiment's baseline is stale.
    from agentos_core.autopilot.versions import apply_manual_edit, lock_agent

    async with unit_of_work() as s:
        await apply_manual_edit(
            s, await lock_agent(s, agent.id), {"tool_strategy": "search first"}, user_id=admin
        )
    with pytest.raises(dep.DeploymentError, match="changed since"):
        async with unit_of_work() as s:
            await dep.request_promotion(s, workspace_id=ws, experiment_id=exp.id, user_id=dev)


async def test_development_split_cannot_be_used_for_experiments(seeded):
    from agentos_core.autopilot.evaluation import EvaluationError

    agent = seeded.agents[EXTRACTION_AGENT]
    async with unit_of_work() as s:
        cand = await propose_manual_candidate(
            s,
            workspace_id=seeded.workspace.id,
            agent_id=agent.id,
            user_id=None,
            proposal=ProposedCandidate(title="t", changes=[ProposedChange(field="tool_strategy", value="x")]),
        )
    with pytest.raises(EvaluationError, match="validation or held-out"):
        async with unit_of_work() as s:
            await start_experiment(
                s,
                workspace_id=seeded.workspace.id,
                agent_id=agent.id,
                candidate_id=cand.id,
                dataset_id=(await ds(seeded.workspace.id, "development")).id,
                trials=1,
                user_id=None,
            )


async def test_diagnosis_rejects_invented_evidence(seeded, monkeypatch):
    from agentos_core.providers import factory
    from agentos_core.providers.mock import MockProvider

    class Fabricator(MockProvider):
        def _diagnosis(self, messages, tools, config):
            import json

            return json.dumps(
                {
                    "failure_category": "OUTPUT_SCHEMA_INVALID",
                    "hypothesis": "The model was confused.",
                    "evidence_ids": ["event:999999999", "signal:" + str(uuid.uuid4())],
                    "confidence": "high",
                    "proposed_remedy": "Rewrite everything.",
                    "limitations": [],
                }
            ), []

    agent = seeded.agents[EXTRACTION_AGENT]
    async with unit_of_work() as s:
        v1 = await s.get(AgentConfigVersion, agent.active_version_id)
        ev = await create_evaluation_run(
            s,
            agent=agent,
            version=v1,
            dataset=await ds(seeded.workspace.id, "development"),
            trials=1,
            created_by=None,
            max_concurrency=6,
            per_case_max_cost_usd=0.25,
        )
    async with running_workers(1, concurrency=6):
        await wait_eval(ev.id)
    monkeypatch.setitem(factory._cache, "mock", Fabricator())
    async with unit_of_work() as s:
        await analyze(s, seeded.workspace.id, window_hours=1)
        d = await diagnose(
            s,
            workspace_id=seeded.workspace.id,
            agent_id=agent.id,
            category="OUTPUT_SCHEMA_INVALID",
            user_id=None,
        )
    assert d.status == "invalid" and "does not exist" in d.validation_errors[0]
    async with unit_of_work() as s:
        with pytest.raises(ValueError, match="valid hypothesis"):
            await generate_candidates(
                s, workspace_id=seeded.workspace.id, agent_id=agent.id, diagnosis_id=d.id, user_id=None
            )


async def test_cycle_spend_limit_stops_autopilot_calls(seeded):
    from agentos_core.db.models import Workspace
    from agentos_core.domain.errors import BudgetExceededError

    async with unit_of_work() as s:
        w = await s.get(Workspace, seeded.workspace.id)
        w.settings = {
            **w.settings,
            "autopilot": {**w.settings.get("autopilot", {}), "max_cycle_spend_usd": 0.01},
        }
    agent = seeded.agents[EXTRACTION_AGENT]
    with pytest.raises(BudgetExceededError, match="cycle spend"):
        async with unit_of_work() as s:
            await generate_candidates(
                s, workspace_id=seeded.workspace.id, agent_id=agent.id, diagnosis_id=None, user_id=None
            )
