"""Distributed tracing: HTTP request -> run -> step attempt -> model call -> tool call -> approval, joined by one trace id."""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import select

from agentos_core.db.models import ApprovalRequest, Event, WorkflowRun
from agentos_core.demo import research_report_workflow
from agentos_core.domain.states import ApprovalStatus
from agentos_core.engine import core
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import pending_approvals, running_workers, start_run, wait_run_status

pytestmark = pytest.mark.integration


@pytest.fixture
def spans():
    provider = trace.get_tracer_provider()
    if not isinstance(provider, TracerProvider):
        provider = TracerProvider()
        trace.set_tracer_provider(provider)
    exporter = InMemorySpanExporter()
    processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(processor)
    yield exporter
    processor.shutdown()


def hexid(n, width):
    return format(n, f"0{width}x")


@pytest.mark.timeout(120)
async def test_one_trace_joins_request_run_attempts_model_tool_and_approval(seeded, spans):
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("http.request") as http:  # stands in for the instrumented HTTP request
        http_ctx = http.get_span_context()
        run_id = await start_run(seeded, research_report_workflow(), name="traced")
    trace_id = hexid(http_ctx.trace_id, 32)

    async with unit_of_work() as s:
        run = await s.get(WorkflowRun, run_id)
    assert run.trace_id == trace_id and run.traceparent.startswith(
        f"00-{trace_id}-"
    )  # the run joined the request's trace

    async with running_workers(2, concurrency=4):
        await wait_run_status(run_id, {"waiting_approval"}, timeout=60)
        (approval,) = await pending_approvals(run_id)
        async with unit_of_work() as s:  # the decision comes from another request/trace
            await core.resolve_approval(
                s,
                approval.id,
                decision=ApprovalStatus.APPROVED,
                user_id=seeded.users["admin"].id,
                reason="ok",
            )
        await wait_run_status(run_id, {"succeeded"}, timeout=60)

    finished = {s.context.span_id: s for s in spans.get_finished_spans()}
    mine = [
        s for s in finished.values() if s.attributes and s.attributes.get("agentos.run_id") == str(run_id)
    ]
    by_name = {}
    for s in mine:
        by_name.setdefault(s.name, []).append(s)
    for name in ("run.create", "step.execute", "model.call", "tool.call"):
        assert name in by_name, name
    assert all(
        hexid(s.context.trace_id, 32) == trace_id for s in mine
    )  # every span of this run is in the request's trace

    root = by_name["run.create"][0]
    assert (
        root.parent is not None and root.parent.span_id == http_ctx.span_id
    )  # the run hangs off the request span
    assert run.traceparent.split("-")[2] == hexid(root.context.span_id, 16)
    steps = by_name["step.execute"]
    assert all(
        s.parent.span_id == root.context.span_id for s in steps
    )  # attempts are children of the run's root span
    assert all(s.attributes["agentos.attempt_id"] for s in steps)
    step_ids = {s.context.span_id for s in steps}
    assert all(
        s.parent.span_id in step_ids for s in by_name["model.call"] + by_name["tool.call"]
    )  # calls nest under attempts

    # The approval decision happened in its own trace and links back to the run's.
    resolve = [s for s in finished.values() if s.name == "approval.resolve"]
    assert len(resolve) == 1 and resolve[0].attributes["agentos.approval_id"] == str(approval.id)
    assert [hexid(link.context.trace_id, 32) for link in resolve[0].links] == [trace_id]

    # Stored events carry the same trace id (so the Inspector, Flight Recorder and any OTel backend agree).
    async with unit_of_work() as s:
        events = (await s.execute(select(Event).where(Event.run_id == run_id))).scalars().all()
        assert (await s.get(ApprovalRequest, approval.id)).status == "approved"
    worker_events = [
        e
        for e in events
        if e.event_type in ("step.started", "model.call.started", "tool.started", "step.completed")
    ]
    assert worker_events and {e.trace_id for e in worker_events} == {trace_id}
    assert len({e.span_id for e in worker_events}) > 3  # distinct spans, one trace
