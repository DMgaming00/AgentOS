"""Flight Recorder end to end on real PostgreSQL: capture, immutability, recorded playback, deterministic
re-execution (side-effect free), divergence detection, comparison, export."""

import json

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from agentos_core.db.models import Artifact, Event, ModelCallRecord, ModelUsage, ToolExecution, WorkflowRun
from agentos_core.demo import research_report_workflow
from agentos_core.domain.states import ApprovalStatus
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.flight.diff import diff_recordings
from agentos_core.flight.export import build_export
from agentos_core.flight.replay import start_reexecution
from agentos_core.flight.timeline import load_recording, state_at
from agentos_core.providers.base import Message, ModelConfig
from agentos_core.providers.recorded import RecordedProvider, ReplayDivergenceError
from agentos_core.testing import (
    get_run,
    get_steps,
    pending_approvals,
    running_workers,
    start_run,
    wait_for,
    wait_run_status,
)

pytestmark = pytest.mark.integration
SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"


async def approve_all_until_done(run_id, seeded, timeout=60):
    async def step():
        run = await get_run(run_id)
        if run.status in ("succeeded", "failed", "cancelled"):
            return run
        for a in await pending_approvals(run_id):
            async with unit_of_work() as s:
                await core.resolve_approval(
                    s, a.id, decision=ApprovalStatus.APPROVED, user_id=seeded.users["admin"].id, reason="ok"
                )
        return None

    return await wait_for(step, timeout=timeout, interval=0.3)


async def recorded_research_run(seeded):
    run_id = await start_run(
        seeded,
        research_report_workflow(),
        name="research-report",
        run_input={"note": f"never store {SECRET}"},
    )
    async with running_workers(2):
        run = await approve_all_until_done(run_id, seeded)
    assert run.status == "succeeded", run.error
    return run_id


async def test_every_model_call_is_recorded_sanitized_and_immutable(seeded):
    run_id = await recorded_research_run(seeded)
    async with unit_of_work() as s:
        records = (
            (
                await s.execute(
                    select(ModelCallRecord)
                    .where(ModelCallRecord.run_id == run_id)
                    .order_by(ModelCallRecord.created_at)
                )
            )
            .scalars()
            .all()
        )
        usage = (
            await s.execute(select(func.count()).select_from(ModelUsage).where(ModelUsage.run_id == run_id))
        ).scalar_one()
        done = (
            (
                await s.execute(
                    select(Event).where(Event.run_id == run_id, Event.event_type == "model.call.completed")
                )
            )
            .scalars()
            .all()
        )
    assert len(records) == usage > 5  # one record per model call, same transaction as the usage row
    assert {str(r.id) for r in records} == {
        e.payload["record_id"] for e in done
    }  # events point at their record
    assert all(r.status == "succeeded" and r.provider == "mock" and r.request_digest for r in records)
    first = records[0]
    assert first.request["system"] and first.request["messages"][0]["role"] == "user"
    assert first.request["meta"]["agent_version_id"] and first.request["meta"]["node_prompt_sha256"]
    assert SECRET not in json.dumps([r.request for r in records]) + json.dumps([r.response for r in records])
    assert first.agent_version_id is not None
    # evidence cannot be rewritten or removed, even by buggy code
    for sql in ("UPDATE model_call_records SET model = 'x'", "DELETE FROM model_call_records"):
        with pytest.raises(DBAPIError, match="immutable audit record"):
            async with unit_of_work() as s:
                await s.execute(text(sql))


async def test_recording_has_contiguous_frames_and_reconstructs_final_state(seeded):
    run_id = await recorded_research_run(seeded)
    async with unit_of_work() as s:
        rec = await load_recording(s, run_id)
    integ = rec["integrity"]
    assert integ["contiguous"] and integ["gaps"] == [] and integ["first_seq"] == 1
    assert integ["event_count"] == integ["last_seq"] == len(rec["frames"])
    steps = await get_steps(run_id)
    assert state_at(rec, integ["last_seq"]) == {k: s.status for k, s in steps.items()}
    assert state_at(rec, 0) == {k: "pending" for k in steps}  # before anything happened
    assert any(f["type"] == "approval.requested" for f in rec["frames"])
    assert all(f["trace_id"] is None or len(f["trace_id"]) == 32 for f in rec["frames"])
    assert rec["mode"] == "recorded" and rec["run"]["purpose"] == "workflow"
    assert rec["approvals"] and rec["model_calls"] and rec["tool_executions"] and rec["attempts"]
    # recorded replay is read-only: loading it again changes nothing
    async with unit_of_work() as s:
        before = (await s.execute(select(func.count()).select_from(WorkflowRun))).scalar_one()
        await load_recording(s, run_id)
        assert (await s.execute(select(func.count()).select_from(WorkflowRun))).scalar_one() == before


async def test_failures_are_highlighted_in_the_recording(seeded):
    run_id = await start_run(
        seeded,
        {
            "nodes": [
                {
                    "key": "boom",
                    "type": "tool_task",
                    "tool": "calculator",
                    "retry": {"max_attempts": 1},
                    "arguments": {"operation": "divide", "values": [1, 0]},
                }
            ]
        },
    )
    async with running_workers(1):
        run = await wait_run_status(run_id, {"failed", "succeeded"}, 20)
    assert run.status == "failed"
    async with unit_of_work() as s:
        rec = await load_recording(s, run_id)
    errors = [f["type"] for f in rec["frames"] if f["severity"] == "error"]
    assert "step.failed" in errors and "run.failed" in errors
    assert state_at(rec, rec["integrity"]["last_seq"])["boom"] == "failed"


async def test_deterministic_reexecution_reproduces_the_run_without_side_effects(seeded):
    src_id = await recorded_research_run(seeded)
    async with unit_of_work() as s:
        src_artifacts = (
            await s.execute(select(func.count()).select_from(Artifact).where(Artifact.run_id == src_id))
        ).scalar_one()
        replay = await start_reexecution(s, src_id, user_id=seeded.users["developer"].id)
        replay_id = replay.id
    assert src_artifacts == 1 and replay.purpose == "replay" and replay.replay_of_run_id == src_id
    async with running_workers(2):
        run = await wait_run_status(replay_id, {"succeeded", "failed"}, timeout=60)
    assert run.status == "succeeded", run.error
    src_steps, rep_steps = await get_steps(src_id), await get_steps(replay_id)
    # same agent versions, same analysis result, same final report text
    assert {k: s.agent_version_id for k, s in rep_steps.items()} == {
        k: s.agent_version_id for k, s in src_steps.items()
    }
    assert rep_steps["analyze"].output == src_steps["analyze"].output
    async with unit_of_work() as s:
        usage = (await s.execute(select(ModelUsage).where(ModelUsage.run_id == replay_id))).scalars().all()
        calls = (
            (
                await s.execute(
                    select(Event).where(Event.run_id == replay_id, Event.event_type == "model.call.completed")
                )
            )
            .scalars()
            .all()
        )
        artifacts = (
            await s.execute(select(func.count()).select_from(Artifact).where(Artifact.run_id == replay_id))
        ).scalar_one()
        denied = (
            (
                await s.execute(
                    select(ToolExecution).where(
                        ToolExecution.run_id == replay_id, ToolExecution.status == "denied"
                    )
                )
            )
            .scalars()
            .all()
        )
        approvals_made = len(await pending_approvals(replay_id))
        mock_calls = (
            await s.execute(
                select(func.count())
                .select_from(ModelCallRecord)
                .where(ModelCallRecord.run_id == replay_id, ModelCallRecord.provider == "mock")
            )
        ).scalar_one()
    assert (
        usage and {u.provider for u in usage} == {"recorded"} and mock_calls == 0
    )  # no provider was involved
    assert artifacts == 0 and approvals_made == 0  # the gated write was refused, nothing was requested
    assert [d.tool_name for d in denied] == ["artifact_writer"]  # the unsafe action is refused and recorded
    # Every model request is identical to the recorded one, except the writer's: its tool list lost
    # artifact_writer and its second request carries the denial instead of the artifact (the divergence is flagged).
    differing = [c.payload["node_key"] for c in calls if not c.payload["replay"]["request_matches_recording"]]
    assert differing == ["write_report", "write_report"] and len(calls) > 4


async def test_reexecution_refuses_runs_that_cannot_be_replayed(seeded):
    run_id = await start_run(seeded, {"nodes": [{"key": "a", "type": "aggregation"}]})
    async with unit_of_work() as s:
        with pytest.raises(core.EngineError, match="finished"):
            await start_reexecution(s, run_id, user_id=None)  # still running
    async with running_workers(1):
        await wait_run_status(run_id, {"succeeded"}, 20)
    async with unit_of_work() as s:
        replay = await start_reexecution(
            s, run_id, user_id=None
        )  # tool/aggregation-only runs need no recordings
        replay_id = replay.id
    async with running_workers(1):
        await wait_run_status(replay_id, {"succeeded"}, 20)
    async with unit_of_work() as s:
        with pytest.raises(core.EngineError, match="cannot itself"):
            await start_reexecution(s, replay_id, user_id=None)


async def test_a_replayed_unsafe_tool_task_is_refused(seeded):
    wf = {
        "nodes": [
            {
                "key": "write",
                "type": "tool_task",
                "tool": "artifact_writer",
                "arguments": {"filename": "x.md", "content": "hello"},
            }
        ]
    }
    run_id = await start_run(seeded, wf)
    async with running_workers(1):
        run = await approve_all_until_done(run_id, seeded)
    assert run.status == "succeeded", run.error
    async with unit_of_work() as s:
        replay_id = (await start_reexecution(s, run_id, user_id=None)).id
    async with running_workers(1):
        run = await wait_run_status(replay_id, {"succeeded", "failed"}, 20)
    assert run.status == "failed"
    err = (await get_steps(replay_id))["write"].error
    assert (
        err["type"] == "policy_violation" and "not allowed in a deterministic re-execution" in err["message"]
    )
    async with unit_of_work() as s:
        assert (
            await s.execute(select(func.count()).select_from(Artifact).where(Artifact.run_id == replay_id))
        ).scalar_one() == 0


async def test_recorded_provider_fails_loudly_when_reexecution_asks_for_more_than_was_recorded(seeded):
    run_id = await recorded_research_run(seeded)
    p = RecordedProvider()
    cfg = ModelConfig(
        model="m", max_tokens=10, extra={"replay_run_id": str(run_id), "replay_node_key": "analyze"}
    )
    msgs = [Message(role="user", text="q")]
    first = await p.generate("s", msgs, [], cfg)
    assert first.provider == "recorded" and first.raw["replay"]["record_id"]
    many = msgs + [Message(role="assistant", text="a")] * 50  # far more turns than were ever recorded
    with pytest.raises(ReplayDivergenceError, match="only .* were recorded"):
        await p.generate("s", many, [], cfg)


async def test_comparison_of_original_and_reexecution_is_structured(seeded):
    src_id = await recorded_research_run(seeded)
    async with unit_of_work() as s:
        replay_id = (await start_reexecution(s, src_id, user_id=None)).id
    async with running_workers(2):
        await wait_run_status(replay_id, {"succeeded", "failed"}, 60)
    async with unit_of_work() as s:
        ra, rb = await load_recording(s, src_id), await load_recording(s, replay_id)
        bodies = {}
        for rid in (src_id, replay_id):
            for r in (
                await s.execute(select(ModelCallRecord).where(ModelCallRecord.run_id == rid))
            ).scalars():
                bodies[str(r.id)] = {"request": r.request, "response": r.response}
    d = diff_recordings(ra, rb, bodies, bodies)
    by = {s["key"]: s for s in d["steps"]}
    assert d["summary"]["final_status"] == ["succeeded", "succeeded"] and d["summary"]["steps_total"] == 5
    assert by["analyze"]["differences"] == []  # identical behavior where nothing unsafe happened
    kinds = {x["kind"] for x in by["write_report"]["differences"]}
    assert "tool_calls" in kinds  # the writer's gated tool call is where the two executions diverge


async def test_export_of_a_real_run_contains_no_credentials(seeded):
    run_id = await recorded_research_run(seeded)
    async with unit_of_work() as s:
        rec = await load_recording(s, run_id)
        bodies = {
            str(r.id): {"request": r.request, "response": r.response}
            for r in (
                await s.execute(select(ModelCallRecord).where(ModelCallRecord.run_id == run_id))
            ).scalars()
        }
    pkg = build_export(rec, bodies)
    blob = json.dumps(pkg)
    assert SECRET not in blob and "lease_token" not in blob
    assert pkg["recording"]["run"]["id"] == str(run_id) and pkg["integrity"]["package_sha256"]
