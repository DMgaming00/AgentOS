"""Chaos Lab on real PostgreSQL: faults are injected at the real boundaries and the platform's own retry, policy,
fencing, budget and recovery logic answers them. Worker-crash faults need real processes (see test_chaos_crash)."""

import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from agentos_core.autopilot.versions import ensure_version, lock_agent
from agentos_core.chaos import experiments as chaos_exp
from agentos_core.chaos.compare import compare_experiments
from agentos_core.chaos.experiments import abort_experiment, create_experiment
from agentos_core.chaos.faults import ChaosPlan
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    ApprovalRequest,
    Artifact,
    ChaosExperiment,
    ChaosInjection,
    Event,
    StepAttempt,
    ToolExecution,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.demo import research_report_workflow
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import add_agent, running_workers, save_workflow, wait_for
from agentos_core.util import utcnow

pytestmark = pytest.mark.integration
FAST = {"max_attempts": 3, "backoff_base_seconds": 0.1, "backoff_max_seconds": 0.2}


def research_workflow(retry=FAST):
    return {
        "nodes": [
            {
                "key": "r1",
                "type": "agent_task",
                "agent": "Research Agent",
                "tools": ["document_search"],
                "input": {"platform": "Nimbus Inference"},
                "retry": retry,
            },
            {
                "key": "r2",
                "type": "agent_task",
                "agent": "Research Agent",
                "tools": ["document_search"],
                "input": {"platform": "Helix Serve"},
                "retry": retry,
            },
            {"key": "agg", "type": "aggregation", "depends_on": ["r1", "r2"]},
        ]
    }


def plan(faults, **kw):
    return ChaosPlan.model_validate(
        {
            "faults": faults,
            "trials": kw.pop("trials", 6),
            "seed": kw.pop("seed", 1),
            "duration_seconds": kw.pop("duration_seconds", 60),
            **kw,
        }
    )


async def execute(seeded, definition, p, *, overrides=None, workers=2, timeout=90, name="wf", before=None):
    wf_id = await save_workflow(seeded, f"{name}-{uuid.uuid4().hex[:8]}", definition)
    async with unit_of_work() as s:
        exp = await create_experiment(
            s,
            workspace_id=seeded.workspace.id,
            user_id=seeded.users["developer"].id,
            name="exp",
            workflow_id=wf_id,
            plan=p,
            version_overrides=overrides,
        )
        exp_id = exp.id
    if before:
        await before()
    async with running_workers(workers):

        async def done():
            async with unit_of_work() as s:
                e = await s.get(ChaosExperiment, exp_id)
                return e if e.status != "running" else None

        return await wait_for(done, timeout=timeout, interval=0.3)


def metric(exp, name):
    return exp.results["metrics"][name]


async def injections_of(exp_id):
    async with unit_of_work() as s:
        return (
            (
                await s.execute(
                    select(ChaosInjection)
                    .where(ChaosInjection.experiment_id == exp_id)
                    .order_by(ChaosInjection.injected_at)
                )
            )
            .scalars()
            .all()
        )


async def test_provider_timeouts_are_absorbed_by_retries_and_measured(seeded):
    p = plan([{"type": "provider_timeout", "probability": 1.0}], trials=6)
    exp = await execute(seeded, research_workflow(), p)
    assert exp.status == "completed" and exp.results["partial"] is False
    m = exp.results["metrics"]
    assert m["task_completion_rate"] == {"value": 1.0, "numerator": 6, "denominator": 6}
    assert m["fault_exposure"]["numerator"] == 6 and m["recovery_rate"] == {
        "value": 1.0,
        "numerator": 6,
        "denominator": 6,
    }
    # one timeout per step (the cap is per step so the schedule does not depend on which step runs first)
    assert exp.results["injections_total"] == 12 and exp.results["injections_by_type"] == {
        "provider_timeout": 12
    }
    assert m["retry_amplification"]["attempts_per_step"] > 1  # the cost of recovery
    assert m["time_to_detection_ms"]["n"] == 12 and m["time_to_recovery_ms"]["n"] == 12
    assert m["state_consistency"]["value"] == 1.0 and m["budget_compliance"]["value"] == 1.0
    async with (
        unit_of_work() as s
    ):  # the fault went through the real error path: failed model call, then retry
        failed = (
            await s.execute(
                select(func.count()).select_from(Event).where(Event.event_type == "model.call.failed")
            )
        ).scalar_one()
        retrying = (
            await s.execute(
                select(func.count()).select_from(Event).where(Event.event_type == "step.retrying")
            )
        ).scalar_one()
    assert failed == 12 and retrying == 12


async def test_recovery_policy_no_retry_turns_the_same_faults_into_failures(seeded):
    p = plan([{"type": "provider_timeout", "probability": 1.0}], trials=4, recovery_policy="no_retry")
    exp = await execute(seeded, research_workflow(), p)
    m = metric(exp, "task_completion_rate")
    assert m == {"value": 0.0, "numerator": 0, "denominator": 4}
    assert metric(exp, "recovery_rate")["numerator"] == 0 and metric(exp, "state_consistency")["value"] == 1.0
    assert all(c["error"] == "provider_timeout" for c in exp.results["per_case"])
    # runs that fail on purpose are not production failures: Failure Intelligence must not see them
    from agentos_core.autopilot.failures import analyze
    from agentos_core.db.models import FailureSignal

    async with unit_of_work() as s:
        await analyze(s, seeded.workspace.id, window_hours=2)
        assert (await s.execute(select(func.count()).select_from(FailureSignal))).scalar_one() == 0


async def test_before_after_comparison_under_the_same_fault_schedule(seeded):
    agent = seeded.agents["Research Agent"]
    async with unit_of_work() as s:
        a = await lock_agent(s, agent.id)
        v1 = await s.get(AgentConfigVersion, a.active_version_id)
        cfg = v1.config_snapshot
        from agentos_core.autopilot.config import AgentConfig

        base_cfg = AgentConfig.model_validate(cfg)
        no_retry, _ = await ensure_version(
            s,
            a,
            base_cfg.model_copy(update={"retry_max_attempts": 1}),
            source="manual",
            parent_version_id=v1.id,
            created_by=None,
        )
        with_retry, _ = await ensure_version(
            s,
            a,
            base_cfg.model_copy(update={"retry_max_attempts": 3}),
            source="manual",
            parent_version_id=v1.id,
            created_by=None,
        )
        ids = (no_retry.id, with_retry.id, a.id)
    p = plan([{"type": "provider_timeout", "probability": 1.0}], trials=10)
    before = await execute(seeded, research_workflow(), p, overrides={ids[2]: ids[0]}, name="ab-before")
    after = await execute(seeded, research_workflow(), p, overrides={ids[2]: ids[1]}, name="ab-after")
    assert before.plan_sha256 == after.plan_sha256
    cmp = compare_experiments(before, after)
    paired = cmp["paired_completion"]
    assert (
        paired["baseline_success"] == 0.0 and paired["candidate_success"] == 1.0 and paired["n_matched"] == 10
    )
    assert paired["significant"] is True and paired["ci95"][0] > 0  # evidence, not just a higher number
    assert cmp["deltas"]["task_completion_rate"]["delta"] == 1.0
    assert (
        cmp["deltas"]["retry_amplification"]["candidate"]["attempts_per_step"]
        > cmp["deltas"]["retry_amplification"]["baseline"]["attempts_per_step"]
    )
    # different plans do not face the same schedule: refused
    other = plan([{"type": "provider_timeout", "probability": 1.0}], trials=10, seed=2)
    third = await execute(seeded, research_workflow(), other, name="ab-other")
    with pytest.raises(chaos_exp.ChaosError, match="different plans"):
        compare_experiments(before, third)


async def test_the_fault_schedule_is_reproducible_and_depends_on_the_seed(seeded):
    faults = [{"type": "provider_timeout", "probability": 0.5}, {"type": "tool_failure", "probability": 0.5}]
    schedules = []
    for seed in (4, 4, 5):
        exp = await execute(
            seeded,
            research_workflow(),
            plan(faults, trials=12, seed=seed, max_affected_runs=12),
            name=f"sched{seed}",
        )
        inj = await injections_of(exp.id)
        schedules.append(
            sorted((i.case_index, i.fault_type, i.step_key, i.attempt_number, i.call_key) for i in inj)
        )
    assert schedules[0] == schedules[1] and schedules[0] != schedules[2] and schedules[0]


async def test_max_affected_runs_is_enforced(seeded):
    p = plan([{"type": "provider_timeout", "probability": 1.0}], trials=8, max_affected_runs=3)
    exp = await execute(seeded, research_workflow(), p)
    inj = await injections_of(exp.id)
    assert len({i.case_index for i in inj}) == 3  # only three runs were ever touched
    assert exp.results["metrics"]["fault_exposure"] == {"value": 3 / 8, "numerator": 3, "denominator": 8}
    assert exp.results["metrics"]["task_completion_rate"]["value"] == 1.0


async def test_context_overflow_is_permanent_and_not_retried(seeded):
    p = plan([{"type": "context_overflow", "probability": 1.0}], trials=3)
    exp = await execute(seeded, research_workflow(), p)
    assert metric(exp, "task_completion_rate")["numerator"] == 0
    assert all(c["error"] == "provider_bad_request" for c in exp.results["per_case"])
    assert exp.results["metrics"]["retry_amplification"]["attempts_per_step"] <= 1.5  # no retry storm


async def test_malformed_model_output_is_caught_by_the_contract_and_repaired(seeded):
    await add_agent(seeded, "Echo Agent", scenario="echo", tools=[])
    wf = {
        "nodes": [
            {
                "key": "e",
                "type": "agent_task",
                "agent": "Echo Agent",
                "retry": FAST,
                "output_contract": {"type": "object", "required": ["node"]},
            }
        ]
    }
    p = plan([{"type": "malformed_model_response", "probability": 1.0}], trials=4)
    exp = await execute(seeded, wf, p)
    assert metric(exp, "task_completion_rate")["value"] == 1.0
    async with unit_of_work() as s:
        calls = (
            await s.execute(
                select(func.count()).select_from(Event).where(Event.event_type == "model.call.completed")
            )
        ).scalar_one()
    assert calls == 8  # every run needed the repair turn: 2 model calls instead of 1


async def test_corrupted_responses_containing_nul_characters_do_not_crash_persistence(seeded):
    """Found by the chaos benchmark: a response containing U+0000 failed the INSERT ("unsupported Unicode escape
    sequence") and the step died as an unclassified internal_error. Persisted text is now sanitized."""
    p = plan([{"type": "corrupted_fixture_response", "probability": 1.0}], trials=4)
    exp = await execute(seeded, research_workflow(), p)
    assert exp.status == "completed" and exp.results["injections_by_type"]["corrupted_fixture_response"] >= 4
    errors = {c["error"] for c in exp.results["per_case"]}
    assert "internal_error" not in errors, (
        errors
    )  # corrupted output is rejected or repaired, never an unhandled crash
    assert metric(exp, "state_consistency")["value"] == 1.0
    async with unit_of_work() as s:
        stored = (
            await s.execute(
                select(func.count()).select_from(Event).where(Event.event_type == "model.call.completed")
            )
        ).scalar_one()
    assert stored > 0


async def test_invalid_tool_arguments_and_tool_failures_are_recorded_and_returned_to_the_model(seeded):
    p = plan(
        [
            {"type": "invalid_tool_arguments", "probability": 1.0},
            {"type": "tool_failure", "probability": 1.0},
        ],
        trials=3,
    )
    exp = await execute(seeded, research_workflow(), p)
    async with unit_of_work() as s:
        failed = (
            (await s.execute(select(ToolExecution).where(ToolExecution.status == "failed"))).scalars().all()
        )
    kinds = {(t.error or {}).get("type") for t in failed}
    assert "invalid_tool_arguments" in kinds  # rejected by the real schema validation
    assert exp.status == "completed" and metric(exp, "state_consistency")["value"] == 1.0
    assert exp.results["injections_by_type"].keys() >= {"invalid_tool_arguments"}


async def test_tool_timeout_goes_through_the_real_timeout_path(seeded, monkeypatch):
    from agentos_core.tools.registry import get_registry

    monkeypatch.setattr(get_registry().get("calculator"), "timeout_seconds", 0.3, raising=False)
    wf = {
        "nodes": [
            {
                "key": "c",
                "type": "tool_task",
                "tool": "calculator",
                "retry": {**FAST, "max_attempts": 1},
                "arguments": {"operation": "add", "values": [1, 2]},
            }
        ]
    }
    p = plan([{"type": "tool_timeout", "probability": 1.0}], trials=2)
    exp = await execute(seeded, wf, p)
    async with unit_of_work() as s:
        failed = (
            (await s.execute(select(ToolExecution).where(ToolExecution.status == "failed"))).scalars().all()
        )
    assert len(failed) == 2 and metric(exp, "task_completion_rate")["numerator"] == 0
    assert exp.results["injections_by_type"] == {"tool_timeout": 2}


async def test_hostile_tool_output_cannot_trigger_an_unauthorized_action(seeded):
    await add_agent(seeded, "Gullible Agent", scenario="injection_follower", tools=["document_search"])
    wf = {
        "nodes": [
            {
                "key": "g",
                "type": "agent_task",
                "agent": "Gullible Agent",
                "tools": ["document_search"],
                "retry": FAST,
            }
        ]
    }
    p = plan([{"type": "malicious_tool_result", "probability": 1.0}], trials=5)
    exp = await execute(seeded, wf, p)
    assert metric(exp, "policy_held_under_hostile_output") == {"value": 1.0, "numerator": 5, "denominator": 5}
    async with unit_of_work() as s:
        denied = (
            (
                await s.execute(
                    select(ToolExecution).where(
                        ToolExecution.status == "denied", ToolExecution.tool_name == "artifact_writer"
                    )
                )
            )
            .scalars()
            .all()
        )
        artifacts = (await s.execute(select(func.count()).select_from(Artifact))).scalar_one()
    assert (
        len(denied) == 5 and artifacts == 0
    )  # the model obeyed the injection; the policy refused every attempt
    # control: without the injected result the same agent never asks for the tool
    clean = await execute(
        seeded, wf, plan([{"type": "tool_failure", "probability": 0.0}], trials=2), name="clean-control"
    )
    async with unit_of_work() as s:
        asked = (
            await s.execute(
                select(func.count())
                .select_from(ToolExecution)
                .where(
                    ToolExecution.tool_name == "artifact_writer",
                    ToolExecution.run_id.in_(
                        select(WorkflowRun.id).where(WorkflowRun.chaos_experiment_id == clean.id)
                    ),
                )
            )
        ).scalar_one()
    assert asked == 0


async def test_duplicate_deliveries_do_not_duplicate_side_effects_and_the_reviewer_is_simulated(seeded):
    p = plan(
        [
            {"type": "duplicate_delivery", "point": "after_tool", "probability": 1.0, "max_per_step": 4},
            {"type": "duplicate_delivery", "point": "after_complete", "probability": 1.0, "max_per_step": 4},
        ],
        trials=3,
        duration_seconds=90,
    )
    exp = await execute(seeded, research_report_workflow(), p, timeout=120)
    assert metric(exp, "task_completion_rate")["value"] == 1.0, exp.results["per_case"]
    dup = metric(exp, "duplicate_side_effect_prevention")
    assert dup["denominator"] > 0 and dup["numerator"] == dup["denominator"]
    assert dup["duplicate_successful_executions_found"] == 0
    async with unit_of_work() as s:
        per_run = (await s.execute(select(Artifact.run_id, func.count()).group_by(Artifact.run_id))).all()
        reasons = (await s.execute(select(ApprovalRequest.decision_reason))).scalars().all()
    assert len(per_run) == 3 and all(n == 1 for _, n in per_run)  # exactly one artifact per run
    assert reasons and all((r or "").startswith("chaos: simulated reviewer") for r in reasons)
    outcomes = [i.outcome for i in await injections_of(exp.id) if i.outcome]
    assert any(o["duplicate"] == "step_completion" and o["result"] == "rejected" for o in outcomes)
    assert any(o["duplicate"] == "tool_call" and o["second"] == "idempotent_replay" for o in outcomes)


async def test_a_duplicate_read_only_tool_call_is_replayed_not_re_executed(seeded):
    """Regression found by the duplicate-delivery experiment: repeating a read-only call used to crash the step."""
    from agentos_core.storage import Storage
    from agentos_core.testing import start_run
    from agentos_core.tools.registry import get_registry
    from agentos_core.tools.runner import StepScope, ToolCallRequest, execute_tool_call

    run_id = await start_run(seeded, {"nodes": [{"key": "a", "type": "aggregation"}]})
    async with unit_of_work() as s:
        step = (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_id))).scalar_one()
        attempt = StepAttempt(
            step_id=step.id,
            run_id=run_id,
            attempt_number=1,
            worker_id="w",
            lease_token=uuid.uuid4(),
            status="running",
            started_at=utcnow(),
        )
        s.add(attempt)
        await s.flush()
        scope = StepScope(
            workspace_id=seeded.workspace.id,
            run_id=run_id,
            step_id=step.id,
            node_key="a",
            attempt_id=attempt.id,
            lease_token=attempt.lease_token,
            agent_name=None,
            allowed_tools=frozenset({"calculator"}),
            storage=Storage(),
        )
    call = ToolCallRequest(call_id="c1", name="calculator", arguments={"operation": "add", "values": [2, 3]})
    first = await execute_tool_call(call, scope, get_registry())
    second = await execute_tool_call(call, scope, get_registry())
    assert first.kind == second.kind == "result" and "idempotent_replay" not in first.content
    assert second.content["idempotent_replay"] is True and second.content["result"] == 5.0


async def test_approval_delay_and_rejection(seeded):
    delay = plan(
        [{"type": "approval_delay", "probability": 1.0, "params": {"delay_seconds": 1}}],
        trials=2,
        duration_seconds=90,
    )
    exp = await execute(seeded, research_report_workflow(), delay, timeout=120, name="delay")
    assert metric(exp, "task_completion_rate")["value"] == 1.0
    async with unit_of_work() as s:
        waits = [
            (a.decided_at - a.requested_at).total_seconds()
            for a in (await s.execute(select(ApprovalRequest))).scalars()
        ]
    assert waits and min(waits) >= 1.0
    reject = plan(
        [{"type": "approval_rejection", "probability": 1.0, "max_per_step": 5}], trials=2, duration_seconds=60
    )
    exp = await execute(seeded, research_report_workflow(), reject, timeout=120, name="reject")
    async with unit_of_work() as s:
        statuses = {
            a.status
            for a in (
                await s.execute(
                    select(ApprovalRequest).where(
                        ApprovalRequest.run_id.in_(
                            select(WorkflowRun.id).where(WorkflowRun.chaos_experiment_id == exp.id)
                        )
                    )
                )
            ).scalars()
        }
        written = (
            await s.execute(
                select(func.count())
                .select_from(Artifact)
                .where(
                    Artifact.run_id.in_(
                        select(WorkflowRun.id).where(WorkflowRun.chaos_experiment_id == exp.id)
                    )
                )
            )
        ).scalar_one()
    assert "rejected" in statuses and written == 0  # a rejected action never executes


async def test_budget_exhaustion_stops_runs_before_any_spend(seeded):
    p = plan([{"type": "budget_exhaustion", "probability": 1.0}], trials=3)
    exp = await execute(seeded, research_workflow(), p)
    assert metric(exp, "task_completion_rate")["numerator"] == 0
    assert metric(exp, "budget_compliance") == {"value": 1.0, "numerator": 3, "denominator": 3}
    assert all(c["error"] == "budget_exceeded" for c in exp.results["per_case"])


async def test_infrastructure_faults_do_not_corrupt_state(seeded):
    p = plan(
        [
            {"type": "redis_interruption", "probability": 1.0, "params": {"window_seconds": 1}},
            {"type": "delayed_notifications", "probability": 1.0, "params": {"max_delay_ms": 200}},
        ],
        trials=4,
    )
    exp = await execute(seeded, research_workflow(), p)
    assert (
        metric(exp, "task_completion_rate")["value"] == 1.0
        and metric(exp, "state_consistency")["value"] == 1.0
    )
    assert exp.results["injections_by_type"].keys() >= {"redis_interruption", "delayed_notifications"}


async def test_a_dropped_postgres_connection_is_retried_not_fatal(seeded):
    """Before DatabaseUnavailableError existed, this experiment (probability 1, 6 runs) completed 0 of 6 runs: every
    step using a terminated connection failed permanently as internal_error (state stayed consistent; the work was
    simply never retried). In-process workers share one process, so one termination hits every concurrent step;
    the plan therefore limits exposure and gives the retry policy room, and the assertions are about *kind* of
    failure and overall recovery, not an exact count."""
    p = plan(
        [{"type": "postgres_interruption", "probability": 0.4}],
        trials=8,
        max_affected_runs=3,
        recovery_policy="extra_retries",
    )
    exp = await execute(seeded, research_workflow(), p, timeout=120)
    assert (
        exp.status == "completed" and exp.results["injections_by_type"].get("postgres_interruption", 0) >= 1
    )
    errors = {c["error"] for c in exp.results["per_case"]}
    assert "internal_error" not in errors  # a lost connection is never again treated as a permanent bug
    assert metric(exp, "task_completion_rate")["numerator"] >= 6
    assert metric(exp, "state_consistency")["value"] == 1.0


async def test_window_expiry_cancels_unfinished_runs_and_results_are_sealed(seeded, monkeypatch):
    monkeypatch.setattr(chaos_exp, "GRACE_SECONDS", 1)
    wf = {"nodes": [{"key": "slow", "type": "aggregation", "inject": {"sleep_seconds": 120}}]}
    p = plan([{"type": "tool_failure", "probability": 0.0}], trials=2, duration_seconds=10)
    exp = await execute(seeded, wf, p, timeout=60)
    assert exp.status == "completed" and exp.abort_reason.startswith("window elapsed")
    assert exp.results["metrics"]["task_completion_rate"]["numerator"] == 0
    async with unit_of_work() as s:
        statuses = {
            r.status
            for r in (
                await s.execute(select(WorkflowRun).where(WorkflowRun.chaos_experiment_id == exp.id))
            ).scalars()
        }
    assert statuses == {"cancelled"}
    with pytest.raises(DBAPIError, match="sealed"):  # results cannot be rewritten after the experiment ends
        async with unit_of_work() as s:
            await s.execute(
                text("UPDATE chaos_experiments SET name = 'tampered' WHERE id = :i"), {"i": exp.id}
            )


async def test_abort_and_injection_evidence_is_immutable(seeded):
    wf = {"nodes": [{"key": "slow", "type": "aggregation", "inject": {"sleep_seconds": 120}}]}
    p = plan([{"type": "provider_timeout", "probability": 1.0}], trials=2)
    wf_id = await save_workflow(seeded, "abortable", wf)
    async with unit_of_work() as s:
        exp = await create_experiment(
            s, workspace_id=seeded.workspace.id, user_id=None, name="x", workflow_id=wf_id, plan=p
        )
        exp_id = exp.id
        s.add(
            ChaosInjection(
                experiment_id=exp_id,
                run_id=None,
                case_index=0,
                fault_index=0,
                fault_type="provider_timeout",
                point="before_model_call",
                detail={},
                injected_at=exp.started_at,
            )
        )
    async with unit_of_work() as s:
        done = await abort_experiment(s, exp_id, "tester said stop")
    assert (
        done.status == "cancelled"
        and done.results["partial"] is True
        and done.abort_reason == "tester said stop"
    )
    with pytest.raises(chaos_exp.ChaosError, match="already cancelled"):
        async with unit_of_work() as s:
            await abort_experiment(s, exp_id, "again")
    for sql in ("UPDATE chaos_injections SET fault_type = 'x'", "DELETE FROM chaos_injections"):
        with pytest.raises(DBAPIError, match="immutable"):
            async with unit_of_work() as s:
                await s.execute(text(sql))
    async with unit_of_work() as s:  # the outcome may be set once and only once
        await s.execute(text("UPDATE chaos_injections SET outcome = '{\"a\": 1}'::jsonb"))
    with pytest.raises(DBAPIError, match="immutable"):
        async with unit_of_work() as s:
            await s.execute(text("UPDATE chaos_injections SET outcome = '{\"a\": 2}'::jsonb"))


async def test_safety_budget_counts_spend_not_transient_reservations(seeded):
    wf = {"nodes": [{"key": "slow", "type": "aggregation", "inject": {"sleep_seconds": 120}}]}
    wf_id = await save_workflow(seeded, "safety", wf)
    p = plan([{"type": "provider_timeout", "probability": 0.0}], trials=2, safety_budget_usd=0.01)
    async with unit_of_work() as s:
        exp = await create_experiment(
            s, workspace_id=seeded.workspace.id, user_id=None, name="s", workflow_id=wf_id, plan=p
        )
        exp_id = exp.id
        for r in (
            await s.execute(select(WorkflowRun).where(WorkflowRun.chaos_experiment_id == exp_id))
        ).scalars():
            r.cost_reserved_usd = (
                0.05  # in-flight holds, five times the safety budget, but nothing actually spent
            )
    assert await chaos_exp.chaos_tick() == 0
    async with unit_of_work() as s:
        assert (await s.get(ChaosExperiment, exp_id)).status == "running"
        for r in (
            await s.execute(select(WorkflowRun).where(WorkflowRun.chaos_experiment_id == exp_id))
        ).scalars():
            r.cost_reserved_usd = 0
            r.cost_spent_usd = 0.02  # now real (estimated) spend exceeds the budget
    assert await chaos_exp.chaos_tick() == 1
    async with unit_of_work() as s:
        done = await s.get(ChaosExperiment, exp_id)
        assert (
            done.status == "cancelled"
            and "safety budget exceeded" in done.abort_reason
            and done.results["partial"] is True
        )


async def test_experiments_are_bounded(seeded):
    wf_id = await save_workflow(seeded, "bounded", research_workflow())
    p = plan([{"type": "provider_timeout", "probability": 1.0}], trials=2)
    async with unit_of_work() as s:
        await create_experiment(
            s, workspace_id=seeded.workspace.id, user_id=None, name="1", workflow_id=wf_id, plan=p
        )
        await create_experiment(
            s, workspace_id=seeded.workspace.id, user_id=None, name="2", workflow_id=wf_id, plan=p
        )
    with pytest.raises(chaos_exp.ChaosError, match="at most 2"):
        async with unit_of_work() as s:
            await create_experiment(
                s, workspace_id=seeded.workspace.id, user_id=None, name="3", workflow_id=wf_id, plan=p
            )
    with pytest.raises(chaos_exp.ChaosError, match="unknown node"):
        async with unit_of_work() as s:
            await s.execute(text("UPDATE chaos_experiments SET status = 'cancelled'"))  # free the slots
            await create_experiment(
                s,
                workspace_id=seeded.workspace.id,
                user_id=None,
                name="4",
                workflow_id=wf_id,
                plan=plan([{"type": "provider_timeout", "node_key": "ghost"}]),
            )
    _ = AgentDefinition
