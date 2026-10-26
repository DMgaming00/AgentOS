"""End-to-end workflow execution with in-process workers and the mock provider."""

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from agentos_core.config import get_settings
from agentos_core.db.models import (
    ApprovalRequest,
    Artifact,
    Event,
    ModelUsage,
    StepAttempt,
    ToolExecution,
    WorkflowDefinition,
)
from agentos_core.demo import approval_workflow, research_report_workflow
from agentos_core.domain.states import ApprovalStatus
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.notify import Notifier, set_notifier
from agentos_core.storage import Storage
from agentos_core.testing import (
    add_agent,
    get_run,
    get_steps,
    pending_approvals,
    running_workers,
    start_run,
    wait_for,
    wait_run_status,
)

pytestmark = pytest.mark.integration


async def decide(approval_id, decision, user_id, reason=None):
    async with unit_of_work() as s:
        return await core.resolve_approval(s, approval_id, decision=decision, user_id=user_id, reason=reason)


async def wait_pending_approval(run_id, count=1, timeout=30):
    async def check():
        items = await pending_approvals(run_id)
        return items if len(items) >= count else None

    return await wait_for(check, timeout)


async def count(model, *where):
    async with unit_of_work() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def test_research_report_end_to_end_with_approval(seeded):
    run_id = await start_run(seeded, research_report_workflow(), name="research-report")
    async with running_workers(2):
        (approval,) = await wait_pending_approval(run_id)
        run = await get_run(run_id)
        assert run.status == "waiting_approval"
        assert approval.requested_action.startswith("Write artifact inference-platform-comparison.md")
        assert approval.requesting_agent == "Writer Agent"
        assert "| Median latency (p50) (ms) |" in approval.sanitized_arguments["content"]
        assert await count(Artifact, Artifact.run_id == run_id) == 0  # nothing written before approval
        await decide(approval.id, ApprovalStatus.APPROVED, seeded.users["developer"].id, "looks good")
        run = await wait_run_status(run_id, {"succeeded", "failed"})
    assert run.status == "succeeded", run.error
    steps = await get_steps(run_id)
    assert {s.status for s in steps.values()} == {"succeeded"}
    analysis = steps["analyze"].output
    assert set(analysis["subjects"]) == {"Nimbus Inference", "Helix Serve", "Vector Forge"}
    latency = next(c for c in analysis["comparison"] if c["metric"] == "median_latency_p50")
    assert latency["best"] == "Helix Serve" and latency["mean"] == pytest.approx(193.3333333333)
    assert any(
        m["metric"] == "cold_start_time" and m["subject"] == "Helix Serve"
        for m in analysis["missing_evidence"]
    )
    assert any(c["metric"] == "uptime_sla" for c in analysis["conflicts"])
    # parallel research: all three research steps were running at the same time at some point
    async with unit_of_work() as s:
        attempts = (await s.execute(select(StepAttempt).where(StepAttempt.run_id == run_id))).scalars().all()
        usage = (await s.execute(select(ModelUsage).where(ModelUsage.run_id == run_id))).scalars().all()
        artifact = (await s.execute(select(Artifact).where(Artifact.run_id == run_id))).scalar_one()
    assert len({a.worker_id for a in attempts}) >= 1 and all(
        a.status in ("succeeded", "suspended") for a in attempts
    )
    assert len(usage) >= 8 and all(u.cost_basis == "simulated" and u.input_tokens for u in usage)
    content = Storage().resolve(artifact.storage_path).read_bytes()
    assert artifact.byte_size == len(content) and b"## Conclusions (interpretation)" in content
    assert b"platform-helix.md#" in content
    run = await get_run(run_id)
    assert run.cost_spent_usd > 0 and run.cost_reserved_usd == 0 and run.model_calls == len(usage)


async def test_parallel_steps_overlap_in_time(seeded):
    await add_agent(seeded, "Slow", scenario="echo", tools=[], config={"latency_ms": 600})
    nodes = [{"key": f"p{i}", "type": "agent_task", "agent": "Slow"} for i in range(3)]
    run_id = await start_run(seeded, {"nodes": nodes, "budget": {"max_concurrency": 3}})
    async with running_workers(1, concurrency=3):
        await wait_run_status(run_id, {"succeeded"}, timeout=20)
    async with unit_of_work() as s:
        attempts = (await s.execute(select(StepAttempt).where(StepAttempt.run_id == run_id))).scalars().all()
    latest_start = max(a.started_at for a in attempts)
    earliest_finish = min(a.finished_at for a in attempts)
    assert latest_start < earliest_finish, "steps did not run concurrently"


async def test_rejection_prevents_side_effect_then_resubmission_and_single_artifact(seeded):
    wf = approval_workflow()
    wf["nodes"][0].pop("inject")  # crash-after-tool is covered by the subprocess test
    run_id = await start_run(seeded, wf, name="approval")
    async with running_workers(1):
        (first,) = await wait_pending_approval(run_id)
        await decide(first.id, ApprovalStatus.REJECTED, seeded.users["admin"].id, "add a revision note")
        assert await count(Artifact, Artifact.run_id == run_id) == 0

        async def second_request():
            items = await pending_approvals(run_id)
            return items[0] if items and items[0].id != first.id else None

        second = await wait_for(second_request)
        assert second.tool_execution_id != first.tool_execution_id
        assert "Resubmitted after reviewer feedback" in second.sanitized_arguments["content"]
        with pytest.raises(core.EngineError):
            await decide(
                first.id, ApprovalStatus.APPROVED, seeded.users["admin"].id
            )  # single-use: already decided
        await decide(second.id, ApprovalStatus.APPROVED, seeded.users["admin"].id)
        run = await wait_run_status(run_id, {"succeeded", "failed"})
    assert run.status == "succeeded", run.error
    assert await count(Artifact, Artifact.run_id == run_id) == 1
    async with unit_of_work() as s:
        tes = (
            (
                await s.execute(
                    select(ToolExecution)
                    .where(ToolExecution.run_id == run_id)
                    .order_by(ToolExecution.created_at)
                )
            )
            .scalars()
            .all()
        )
    writer = [
        t.status
        for t in tes
        if t.tool_name == "artifact_writer" and t.step_id == (await get_steps(run_id))["draft_note"].id
    ]
    assert writer == ["rejected", "succeeded"]
    denied = [t for t in tes if t.status == "denied"]
    assert len(denied) == 1 and denied[0].tool_name == "artifact_writer"  # Untrusted Agent was blocked
    steps = await get_steps(run_id)
    assert steps["untrusted_attempt"].output["denied"] is True
    assert await count(Event, Event.run_id == run_id, Event.event_type == "tool.denied") == 1


async def test_approval_is_bound_to_arguments_and_single_use(seeded):
    run_id = await start_run(
        seeded,
        {
            "nodes": [
                {
                    "key": "export",
                    "type": "tool_task",
                    "tool": "artifact_writer",
                    "arguments": {"filename": "x.md", "content": "hello"},
                }
            ]
        },
    )
    async with running_workers(1):
        (ap,) = await wait_pending_approval(run_id)
        async with unit_of_work() as s:
            # altered arguments do not match the approval
            assert not await core.consume_approval(
                s, ap.id, tool_execution_id=ap.tool_execution_id, arguments_hash="0" * 64
            )
        await decide(ap.id, ApprovalStatus.APPROVED, seeded.users["admin"].id)
        await wait_run_status(run_id, {"succeeded"})
    async with unit_of_work() as s:
        assert not await core.consume_approval(
            s, ap.id, tool_execution_id=ap.tool_execution_id, arguments_hash=ap.arguments_hash
        )  # already consumed
        row = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.id == ap.id))).scalar_one()
    assert row.consumed_at is not None


async def test_rejected_tool_task_fails_without_side_effect(seeded):
    run_id = await start_run(
        seeded,
        {
            "nodes": [
                {
                    "key": "export",
                    "type": "tool_task",
                    "tool": "artifact_writer",
                    "arguments": {"filename": "x.md", "content": "hello"},
                }
            ]
        },
    )
    async with running_workers(1):
        (ap,) = await wait_pending_approval(run_id)
        await decide(ap.id, ApprovalStatus.REJECTED, seeded.users["admin"].id, "no")
        run = await wait_run_status(run_id, {"failed", "succeeded"})
    assert run.status == "failed"
    assert (await get_steps(run_id))["export"].error["type"] == "approval_rejected"
    assert await count(Artifact, Artifact.run_id == run_id) == 0


async def test_approval_gate_and_expiry(seeded):
    run_id = await start_run(
        seeded,
        {
            "nodes": [
                {"key": "gate", "type": "approval_gate", "action": "Deploy to production"},
                {"key": "after", "type": "aggregation", "depends_on": ["gate"]},
            ]
        },
    )
    async with running_workers(1):
        (ap,) = await wait_pending_approval(run_id)
        assert ap.tool_execution_id is None and ap.requested_action == "Deploy to production"
        async with unit_of_work() as s:
            await s.execute(
                update(ApprovalRequest)
                .where(ApprovalRequest.id == ap.id)
                .values(expires_at=core.utcnow() - timedelta(seconds=1))
            )
        run = await wait_run_status(run_id, {"failed", "succeeded"})
    assert run.status == "failed"
    async with unit_of_work() as s:
        row = (await s.execute(select(ApprovalRequest).where(ApprovalRequest.id == ap.id))).scalar_one()
    assert row.status == "expired"
    with pytest.raises(core.EngineError):
        await decide(ap.id, ApprovalStatus.APPROVED, seeded.users["admin"].id)


async def test_approval_gate_approved_continues(seeded):
    run_id = await start_run(
        seeded,
        {
            "nodes": [
                {"key": "gate", "type": "approval_gate", "action": "Continue"},
                {"key": "after", "type": "aggregation", "depends_on": ["gate"]},
            ]
        },
    )
    async with running_workers(1):
        (ap,) = await wait_pending_approval(run_id)
        await decide(ap.id, ApprovalStatus.APPROVED, seeded.users["viewer"].id)
        run = await wait_run_status(run_id, {"succeeded", "failed"})
    assert run.status == "succeeded"
    assert (await get_steps(run_id))["after"].output["inputs"]["gate"]["approved"] is True


async def test_duplicate_pending_approval_for_step_is_rejected_by_constraint(seeded):
    run_id = await start_run(seeded, {"nodes": [{"key": "gate", "type": "approval_gate", "action": "x"}]})
    async with running_workers(1):
        (ap,) = await wait_pending_approval(run_id)
    with pytest.raises(IntegrityError):
        async with unit_of_work() as s:
            s.add(
                ApprovalRequest(
                    id=uuid.uuid4(),
                    workspace_id=ap.workspace_id,
                    run_id=run_id,
                    step_id=ap.step_id,
                    requested_action="dup",
                    arguments_hash=ap.arguments_hash,
                    status="pending",
                    requested_at=ap.requested_at,
                    expires_at=ap.expires_at,
                )
            )


async def test_provider_rate_limit_retries_then_succeeds(seeded):
    await add_agent(seeded, "Flaky", scenario="rate_limit", tools=[], config={"fail_attempts": 2})
    run_id = await start_run(
        seeded,
        {
            "nodes": [
                {
                    "key": "a",
                    "type": "agent_task",
                    "agent": "Flaky",
                    "retry": {"max_attempts": 3, "backoff_base_seconds": 0.1},
                }
            ]
        },
    )
    async with running_workers(1):
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=20)
    assert run.status == "succeeded"
    async with unit_of_work() as s:
        attempts = (
            (
                await s.execute(
                    select(StepAttempt)
                    .where(StepAttempt.run_id == run_id)
                    .order_by(StepAttempt.attempt_number)
                )
            )
            .scalars()
            .all()
        )
        failed_calls = await s.execute(
            select(func.count())
            .select_from(ModelUsage)
            .where(ModelUsage.run_id == run_id, ModelUsage.status == "failed")
        )
    assert [(a.status, a.error_type) for a in attempts] == [
        ("failed", "provider_rate_limited"),
        ("failed", "provider_rate_limited"),
        ("succeeded", None),
    ]
    assert failed_calls.scalar_one() == 2


async def test_provider_timeout_exhausts_attempts(seeded):
    await add_agent(seeded, "Slowpoke", scenario="timeout", tools=[], config={"fail_attempts": 10})
    run_id = await start_run(
        seeded,
        {
            "nodes": [
                {
                    "key": "a",
                    "type": "agent_task",
                    "agent": "Slowpoke",
                    "retry": {"max_attempts": 2, "backoff_base_seconds": 0.1},
                }
            ]
        },
    )
    async with running_workers(1):
        run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=20)
    assert run.status == "failed" and (await get_steps(run_id))["a"].error["type"] == "provider_timeout"


async def test_cost_budget_prevents_further_model_calls(seeded):
    wf = research_report_workflow()
    wf["budget"] = {"max_model_calls": 2, "max_cost_usd": 2.0, "max_concurrency": 1}
    run_id = await start_run(seeded, wf)
    async with running_workers(1):
        run = await wait_run_status(run_id, {"failed", "succeeded"}, timeout=20)
    assert run.status == "failed"
    errors = [s.error["type"] for s in (await get_steps(run_id)).values() if s.error]
    assert "budget_exceeded" in errors
    assert (await get_run(run_id)).model_calls == 2
    assert await count(Event, Event.run_id == run_id, Event.event_type == "budget.exceeded") >= 1


async def test_invalid_tool_arguments_are_reported_not_executed(seeded):
    from agentos_core.tools.registry import get_registry
    from agentos_core.tools.runner import StepScope, ToolCallRequest, execute_tool_call

    run_id = await start_run(seeded, {"nodes": [{"key": "a", "type": "aggregation"}]})
    (c,) = await core.claim_steps("w", 1)
    scope = StepScope(
        workspace_id=seeded.workspace.id,
        run_id=run_id,
        step_id=c.step_id,
        node_key="a",
        attempt_id=c.attempt_id,
        lease_token=c.lease_token,
        agent_name="x",
        allowed_tools=frozenset({"calculator"}),
        storage=Storage(),
    )
    out = await execute_tool_call(
        ToolCallRequest("1", "calculator", {"operation": "nope"}), scope, get_registry()
    )
    assert out.kind == "error" and "invalid arguments" in out.content["error"]
    out = await execute_tool_call(
        ToolCallRequest("2", "artifact_writer", {"filename": "a.md", "content": "x"}), scope, get_registry()
    )
    assert out.kind == "denied"
    out = await execute_tool_call(ToolCallRequest("3", "shell", {"cmd": "rm -rf /"}), scope, get_registry())
    assert out.kind == "denied"
    out = await execute_tool_call(
        ToolCallRequest("4", "calculator", {"operation": "sum", "values": [1, 2.5]}), scope, get_registry()
    )
    assert out.kind == "result" and out.content["result"] == 3.5
    async with unit_of_work() as s:
        statuses = sorted(
            (await s.execute(select(ToolExecution.status).where(ToolExecution.run_id == run_id))).scalars()
        )
    assert statuses == ["denied", "denied", "failed", "succeeded"]


async def test_artifact_writer_idempotent_under_duplicate_dispatch(seeded):
    from agentos_core.tools.artifact_writer import ArtifactWriterArgs, ArtifactWriterTool
    from agentos_core.tools.base import ToolContext

    run_id = await start_run(seeded, {"nodes": [{"key": "a", "type": "aggregation"}]})
    (c,) = await core.claim_steps("w", 1)
    te_id = uuid.uuid4()
    async with unit_of_work() as s:
        s.add(
            ToolExecution(
                id=te_id,
                run_id=run_id,
                step_id=c.step_id,
                tool_name="artifact_writer",
                arguments={},
                arguments_hash="h",
                status="running",
                idempotency_key="idem-1",
            )
        )
    ctx = ToolContext(
        workspace_id=seeded.workspace.id,
        run_id=run_id,
        step_id=c.step_id,
        node_key="a",
        attempt_id=c.attempt_id,
        lease_token=c.lease_token,
        tool_execution_id=te_id,
        idempotency_key="idem-1",
        storage=Storage(),
    )
    args = ArtifactWriterArgs(filename="dup.md", content="same")
    results = await asyncio.gather(*(ArtifactWriterTool().run(args, ctx) for _ in range(5)))
    assert len({r["artifact_id"] for r in results}) == 1
    assert sum(not r["deduplicated"] for r in results) == 1
    assert await count(Artifact, Artifact.run_id == run_id) == 1
    # a stale lease holder cannot write at all
    from agentos_core.domain.errors import LeaseLostError

    stale = ToolContext(**{**ctx.__dict__, "lease_token": uuid.uuid4(), "idempotency_key": "idem-2"})
    with pytest.raises(LeaseLostError):
        await ArtifactWriterTool().run(ArtifactWriterArgs(filename="other.md", content="x"), stale)


async def test_planned_run_materializes_validated_dag(seeded):
    async with unit_of_work() as s:
        wf = WorkflowDefinition(
            id=uuid.uuid4(),
            workspace_id=seeded.workspace.id,
            name="adhoc",
            version=1,
            definition={"mode": "planned"},
        )
        s.add(wf)
        await s.flush()
        run = await core.create_planning_run(
            s,
            workflow=wf,
            task="Compare three AI inference platforms",
            run_input={"planner_scenario": "invalid_then_valid"},
            requested_by=None,
            budget={"max_model_calls": 30, "max_cost_usd": 2, "max_concurrency": 3},
        )
        run_id = run.id
    assert (await get_run(run_id)).status == "planning"
    async with running_workers(1):
        await wait_pending_approval(run_id)
        steps = await get_steps(run_id)
        planner = steps.pop(core.PLANNER_NODE_KEY)
        assert (
            planner.output["planner_attempts"] == 2
            and planner.output["attempt_issues"][0][0]["code"] == "cycle"
        )
        assert sorted(steps) == [
            "analyze",
            "research_helix_serve",
            "research_nimbus_inference",
            "research_vector_forge",
            "write_report",
        ]
        assert (await get_run(run_id)).budget["max_concurrency"] == 3


async def test_invalid_plan_fails_run(seeded):
    async with unit_of_work() as s:
        wf = WorkflowDefinition(
            id=uuid.uuid4(),
            workspace_id=seeded.workspace.id,
            name="adhoc2",
            version=1,
            definition={"mode": "planned"},
        )
        s.add(wf)
        await s.flush()
        run = await core.create_planning_run(
            s,
            workflow=wf,
            task="x",
            run_input={"planner_scenario": "invalid_plan"},
            requested_by=None,
            budget={"max_model_calls": 30},
        )
        run_id = run.id
    async with running_workers(1):
        run = await wait_run_status(run_id, {"failed", "succeeded"}, timeout=20)
    assert run.status == "failed"
    err = (await get_steps(run_id))[core.PLANNER_NODE_KEY].error
    assert err["type"] == "invalid_model_output" and len(err["issues"]) == 3


async def test_works_without_redis(seeded):
    set_notifier(Notifier("redis://127.0.0.1:1/0"))  # nothing listens here
    try:
        nodes = [
            {
                "key": "a",
                "type": "tool_task",
                "tool": "calculator",
                "arguments": {"operation": "sum", "values": [1]},
            },
            {"key": "b", "type": "aggregation", "depends_on": ["a"]},
        ]
        run_id = await start_run(seeded, {"nodes": nodes})
        async with running_workers(1):
            run = await wait_run_status(run_id, {"succeeded", "failed"}, timeout=20)
        assert run.status == "succeeded"
    finally:
        set_notifier(None)


async def test_cancel_running_agent_step_cooperatively(seeded):
    await add_agent(seeded, "VerySlow", scenario="echo", tools=[], config={"latency_ms": 20_000})
    run_id = await start_run(seeded, {"nodes": [{"key": "a", "type": "agent_task", "agent": "VerySlow"}]})
    async with running_workers(1):

        async def running():
            return (await get_steps(run_id))["a"].status == "running"

        await wait_for(running)
        async with unit_of_work() as s:
            await core.cancel_run(s, run_id, user_id=None)

        async def cancelled():
            return (await get_steps(run_id))["a"].status == "cancelled"

        await wait_for(cancelled, timeout=10)
    run = await get_run(run_id)
    assert run.status == "cancelled" and run.completed_at is not None


def test_settings_use_test_database():
    assert get_settings().database_url.endswith("/agentos_test")
    assert Path(get_settings().artifact_dir).name.startswith("agentos-test-artifacts")
