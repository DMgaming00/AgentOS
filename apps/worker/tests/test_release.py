"""Release Center on real PostgreSQL: environments, gated staged canary, seeded routing, deterministic rollback."""

import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from agentos_core.autopilot import deployment as dep
from agentos_core.autopilot.demo import live_extraction_workflow
from agentos_core.autopilot.versions import canary_bucket, resolve_pins
from agentos_core.db.models import (
    AgentDefinition,
    AgentDeployment,
    ReleaseEvent,
    ReleasePlan,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.engine.tx import unit_of_work
from agentos_core.release import controller as rc
from agentos_core.testing import running_workers, start_run, wait_run_status
from agentos_core.testing_autopilot import clean_security_run, eligible_experiment
from agentos_core.util import utcnow

pytestmark = pytest.mark.integration


async def fake_live_steps(seeded, agent_id, version_id, n, ok):
    """Finished live steps pinned to a version: a traffic fixture for the rule engine. (The loader and the gates are
    exercised with real worker traffic in the promotion test; this lets a test choose what the canary did.)"""
    spec = {"nodes": [{"key": "a", "type": "aggregation"}]}
    for _ in range(n):
        run_id = await start_run(seeded, spec)
        async with unit_of_work() as s:
            now = utcnow()
            await s.execute(
                update(WorkflowStep)
                .where(WorkflowStep.run_id == run_id)
                .values(
                    agent_definition_id=agent_id,
                    agent_version_id=version_id,
                    status="succeeded" if ok else "failed",
                    started_at=now - timedelta(seconds=1),
                    completed_at=now,
                    error=None if ok else {"type": "invalid_model_output", "message": "x"},
                )
            )


async def active_release(seeded, agent, exp, **kw):
    ws, admin, dev = seeded.workspace.id, seeded.users["admin"].id, seeded.users["developer"].id
    await clean_security_run(ws)
    async with unit_of_work() as s:
        plan = await rc.create_release(
            s,
            workspace_id=ws,
            agent_id=agent.id,
            user_id=dev,
            environment="production",
            experiment_id=exp.id,
            schedule=[50, 100],
            seed="seed-R",
            min_stage_samples=4,
            **kw,
        )
        plan_id, request_id = plan.id, plan.request_id
    async with unit_of_work() as s:
        await dep.decide_request(
            s, workspace_id=ws, request_id=request_id, approve=True, user_id=admin, reason="go"
        )
    return plan_id


@pytest.mark.timeout(500)
async def test_gates_staged_canary_seeded_routing_and_promotion(seeded):
    ws, admin, dev = seeded.workspace.id, seeded.users["admin"].id, seeded.users["developer"].id
    agent, exp = await eligible_experiment(seeded)
    v1 = agent.active_version_id

    # No security evaluation yet: the production gate blocks, naming the failing gate and showing all of them.
    with pytest.raises(rc.ReleaseError) as blocked:
        async with unit_of_work() as s:
            await rc.create_release(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                user_id=dev,
                environment="production",
                experiment_id=exp.id,
                schedule=[50, 100],
                seed="seed-A",
                min_stage_samples=4,
            )
    assert blocked.value.code == "gates_failed"
    failed = [g["gate"] for g in blocked.value.details["gates"] if g["passed"] is False]
    assert failed == ["security_evaluation"] and len(blocked.value.details["gates"]) == 7

    await clean_security_run(ws)
    with pytest.raises(rc.ReleaseError) as bad:
        async with unit_of_work() as s:
            await rc.create_release(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                user_id=dev,
                environment="production",
                experiment_id=exp.id,
                schedule=[10, 25],
            )
    assert bad.value.code == "invalid_schedule"
    async with unit_of_work() as s:
        plan = await rc.create_release(
            s,
            workspace_id=ws,
            agent_id=agent.id,
            user_id=dev,
            environment="production",
            experiment_id=exp.id,
            schedule=[50, 100],
            seed="seed-A",
            min_stage_samples=4,
        )
        plan_id, request_id = plan.id, plan.request_id
    with pytest.raises(rc.ReleaseError) as again:  # one live release per agent and environment
        async with unit_of_work() as s:
            await rc.create_release(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                user_id=dev,
                environment="production",
                experiment_id=exp.id,
                schedule=[50, 100],
            )
    assert again.value.code == "release_in_progress"
    async with unit_of_work() as s:
        assert (await s.get(ReleasePlan, plan_id)).status == "pending_approval"
        assert (
            await s.get(AgentDefinition, agent.id)
        ).canary_percent == 0  # nothing routes to the candidate before approval

    async with unit_of_work() as s:
        await dep.decide_request(
            s, workspace_id=ws, request_id=request_id, approve=True, user_id=admin, reason="go"
        )
    async with unit_of_work() as s:
        plan = await s.get(ReleasePlan, plan_id)
        a = await s.get(AgentDefinition, agent.id)
        assert (
            plan.status == "active"
            and a.canary_percent == 50
            and a.canary_seed == "seed-A"
            and a.canary_version_id == plan.candidate_version_id
        )

    # Seeded routing is deterministic and honors the percentage.
    ids = [uuid.uuid4() for _ in range(2000)]
    async with unit_of_work() as s:
        pins = [(await resolve_pins(s, {agent.id}, rid))[agent.id] for rid in ids[:200]]
    expect = [
        plan.candidate_version_id if canary_bucket(rid, agent.id, "seed-A") < 50 else v1 for rid in ids[:200]
    ]
    assert pins == expect
    share = sum(canary_bucket(rid, agent.id, "seed-A") < 50 for rid in ids) / len(ids)
    assert 0.45 < share < 0.55
    assert canary_bucket(ids[0], agent.id, "seed-A") != canary_bucket(
        ids[0], agent.id, "seed-B"
    ) or canary_bucket(ids[1], agent.id, "seed-A") != canary_bucket(ids[1], agent.id, "seed-B")

    # Advancing without live evidence is refused, and the refusal is recorded.
    with pytest.raises(rc.ReleaseError) as early:
        async with unit_of_work() as s:
            await rc.advance(s, plan_id, user_id=admin)
    assert early.value.code == "gates_failed" and "stage_samples" in [
        g["gate"] for g in early.value.details["gates"] if g["passed"] is False
    ]
    async with unit_of_work() as s:
        assert "advance_blocked" in [
            e.event_type
            for e in (await s.execute(select(ReleaseEvent).where(ReleaseEvent.plan_id == plan_id))).scalars()
        ]

    # Real live traffic: each run is pinned to the arm its id selects, and the loader measures both arms.
    run_ids = [await start_run(seeded, live_extraction_workflow(), name=f"live-{i}") for i in range(24)]
    async with running_workers(3, concurrency=6):
        for rid in run_ids:
            await wait_run_status(rid, {"succeeded", "failed"}, timeout=90)
    async with unit_of_work() as s:
        runs = {
            r.id: r
            for r in (await s.execute(select(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))).scalars()
        }
        steps = {
            x.run_id: x
            for x in (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id.in_(run_ids)))).scalars()
        }
        for rid in run_ids:
            want = plan.candidate_version_id if canary_bucket(rid, agent.id, "seed-A") < 50 else v1
            assert steps[rid].agent_version_id == want and runs[rid].environment == "production"
        ev = await rc.evaluate_plan(s, await s.get(ReleasePlan, plan_id))
    assert ev["canary_steps"] >= 4 and ev["baseline_steps"] >= 4
    status = {r["rule"]: r["status"] for r in ev["rules"]}
    assert (
        status["failure_rate_increase"] in ("ok", "insufficient_evidence") and not ev["triggered"]
    )  # the fix is better, not worse

    # Stage gates pass; the final advance promotes the candidate through the normal deployment records.
    async with unit_of_work() as s:
        plan = await rc.advance(s, plan_id, user_id=admin)
    assert plan.status == "completed" and plan.end_reason == "promoted to 100%"
    async with unit_of_work() as s:
        a = await s.get(AgentDefinition, agent.id)
        assert (
            a.active_version_id == plan.candidate_version_id
            and a.canary_version_id is None
            and a.canary_percent == 0
            and a.canary_seed is None
        )
        types = [
            d.deployment_type
            for d in (
                await s.execute(
                    select(AgentDeployment)
                    .where(AgentDeployment.agent_id == agent.id)
                    .order_by(AgentDeployment.deployed_at)
                )
            ).scalars()
        ]
        assert types[-2:] == ["canary", "promotion"]
        events = [
            e.event_type
            for e in (
                await s.execute(
                    select(ReleaseEvent).where(ReleaseEvent.plan_id == plan_id).order_by(ReleaseEvent.id)
                )
            ).scalars()
        ]
        assert (
            events[:3] == ["gates_evaluated", "release_requested", "stage_started"]
            and events[-1] == "completed"
            and "advance_blocked" in events
        )
    # runs that started during the canary kept their pinned versions
    async with unit_of_work() as s:
        for rid in run_ids:
            step = (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id == rid))).scalar_one()
            assert step.agent_version_id == (
                plan.candidate_version_id if canary_bucket(rid, agent.id, "seed-A") < 50 else v1
            )


async def plan_state(plan_id):
    async with unit_of_work() as s:
        plan = await s.get(ReleasePlan, plan_id)
        agent = await s.get(AgentDefinition, plan.agent_id)
        events = [
            (e.event_type, e.payload)
            for e in (
                await s.execute(
                    select(ReleaseEvent).where(ReleaseEvent.plan_id == plan_id).order_by(ReleaseEvent.id)
                )
            ).scalars()
        ]
        return plan, agent, events


@pytest.mark.timeout(300)
async def test_rules_wait_for_evidence_then_roll_back_automatically(seeded):
    agent, exp = await eligible_experiment(seeded)
    v1 = agent.active_version_id
    plan_id = await active_release(seeded, agent, exp, auto_rollback=True)
    plan, _, _ = await plan_state(plan_id)
    cand = plan.candidate_version_id

    # Three failures on the canary look bad but are below every rule's evidence minimum: nothing happens.
    await fake_live_steps(seeded, agent.id, cand, 3, ok=False)
    await fake_live_steps(seeded, agent.id, v1, 12, ok=True)
    assert await rc.release_tick() == 0
    plan, a, events = await plan_state(plan_id)
    assert (
        plan.status == "active"
        and a.canary_percent == 50
        and not [e for e in events if e[0] == "rule_triggered"]
    )

    # With enough evidence the failure-rate rule fires and the platform rolls back by itself.
    await fake_live_steps(seeded, agent.id, cand, 9, ok=False)
    assert await rc.release_tick() >= 2
    plan, a, events = await plan_state(plan_id)
    assert plan.status == "rolled_back" and plan.end_reason.startswith(
        "automatic rollback: failure_rate_increase"
    )
    assert (
        a.active_version_id == v1
        and a.canary_version_id is None
        and a.canary_percent == 0
        and a.canary_seed is None
    )
    triggered = [p for t, p in events if t == "rule_triggered"]
    assert {t["rule"] for t in triggered} >= {"failure_rate_increase"} and triggered[0]["measured"][
        "canary_failure_rate"
    ] == 1.0
    rolled = next(p for t, p in events if t == "rolled_back")
    assert rolled["automatic"] is True and rolled["evidence"]["canary_steps"] == 12
    async with unit_of_work() as s:
        deps = (
            (await s.execute(select(AgentDeployment).where(AgentDeployment.agent_id == agent.id)))
            .scalars()
            .all()
        )
        assert [d.deployment_type for d in sorted(deps, key=lambda d: d.deployed_at)][-2:] == [
            "canary",
            "rollback",
        ]
        assert next(d for d in deps if d.deployment_type == "canary").status == "rolled_back"
    assert await rc.release_tick() == 0  # nothing left to evaluate


@pytest.mark.timeout(300)
async def test_without_auto_rollback_the_platform_recommends_once_and_a_human_decides(seeded):
    agent, exp = await eligible_experiment(seeded)
    v1 = agent.active_version_id
    plan_id = await active_release(seeded, agent, exp, auto_rollback=False)
    plan, _, _ = await plan_state(plan_id)
    await fake_live_steps(seeded, agent.id, plan.candidate_version_id, 12, ok=False)
    await fake_live_steps(seeded, agent.id, v1, 12, ok=True)
    assert await rc.release_tick() >= 1
    assert await rc.release_tick() == 0  # recorded once per stage and rule, not on every tick
    plan, a, events = await plan_state(plan_id)
    assert plan.status == "active" and a.canary_percent == 50
    assert [t for t, _ in events].count("rollback_recommended") == 1
    with pytest.raises(
        rc.ReleaseError
    ) as blocked:  # the stage gates refuse to advance while a rule is triggered
        async with unit_of_work() as s:
            await rc.advance(s, plan_id, user_id=seeded.users["admin"].id)
    assert "rollback_rules_clear" in [
        g["gate"] for g in blocked.value.details["gates"] if g["passed"] is False
    ]
    async with unit_of_work() as s:
        await rc.rollback_release(
            s, plan_id, user_id=seeded.users["admin"].id, reason="operator decision after recommendation"
        )
    plan, a, events = await plan_state(plan_id)
    assert (
        plan.status == "rolled_back"
        and a.active_version_id == v1
        and [p for t, p in events if t == "rolled_back"][0]["automatic"] is False
    )


@pytest.mark.timeout(300)
async def test_environments_route_runs_and_gates_apply_per_environment(seeded):
    ws, dev = seeded.workspace.id, seeded.users["developer"].id
    agent, exp = await eligible_experiment(seeded)
    v1 = agent.active_version_id
    cand = exp.candidate_version_id

    # development: pinned without gates; staging: needs the gates (here the security evaluation is missing)
    async with unit_of_work() as s:
        await rc.create_release(
            s,
            workspace_id=ws,
            agent_id=agent.id,
            user_id=dev,
            environment="development",
            candidate_version_id=cand,
        )
    with pytest.raises(rc.ReleaseError) as no_sec:
        async with unit_of_work() as s:
            await rc.create_release(
                s,
                workspace_id=ws,
                agent_id=agent.id,
                user_id=dev,
                environment="staging",
                experiment_id=exp.id,
            )
    assert no_sec.value.code == "gates_failed"
    await clean_security_run(ws)
    async with unit_of_work() as s:
        stage = await rc.create_release(
            s, workspace_id=ws, agent_id=agent.id, user_id=dev, environment="staging", experiment_id=exp.id
        )
        assert stage.status == "completed"

    async with unit_of_work() as s:
        for env, expected in (("development", cand), ("staging", cand), ("production", v1)):
            assert (await resolve_pins(s, {agent.id}, uuid.uuid4(), None, env))[agent.id] == expected
    # a run created in an environment is pinned to that environment's version and records the environment
    spec = live_extraction_workflow()
    run_stage = await start_run(seeded, spec, name="staging-run", environment="staging")
    run_prod = await start_run(seeded, spec, name="prod-run")
    async with unit_of_work() as s:
        st = (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_stage))).scalar_one()
        pr = (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_prod))).scalar_one()
        assert (await s.get(WorkflowRun, run_stage)).environment == "staging" and (
            await s.get(WorkflowRun, run_prod)
        ).environment == "production"
        assert st.agent_version_id == cand and pr.agent_version_id == v1
    # a dev/staging release does not block a production release for the same agent
    await active_release(seeded, agent, exp)


@pytest.mark.timeout(300)
async def test_a_rejected_request_aborts_the_release(seeded):
    ws, admin, dev = seeded.workspace.id, seeded.users["admin"].id, seeded.users["developer"].id
    agent, exp = await eligible_experiment(seeded)
    await clean_security_run(ws)
    async with unit_of_work() as s:
        plan = await rc.create_release(
            s, workspace_id=ws, agent_id=agent.id, user_id=dev, environment="production", experiment_id=exp.id
        )
        plan_id, req_id = plan.id, plan.request_id
    async with unit_of_work() as s:
        await dep.decide_request(
            s, workspace_id=ws, request_id=req_id, approve=False, user_id=admin, reason="not now"
        )
    plan, a, events = await plan_state(plan_id)
    assert plan.status == "aborted" and "rejected" in plan.end_reason and a.canary_percent == 0
    assert events[-1][0] == "aborted"
    async with unit_of_work() as s:  # the slot is free again
        await rc.create_release(
            s, workspace_id=ws, agent_id=agent.id, user_id=dev, environment="production", experiment_id=exp.id
        )
