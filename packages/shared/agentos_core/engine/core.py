"""Durable workflow engine: every state change of runs, steps and approvals.

Correctness properties (each is covered by an integration test):

* Claims use ``FOR UPDATE SKIP LOCKED`` so two workers never claim the same step.
* Every claim issues a fresh ``lease_token``. Completion, failure and suspension
  writes are *fenced*: they only apply if the caller still holds the current
  token, so a worker that lost its lease can never overwrite a newer result.
* Completed steps are terminal; recovery only touches ``running`` steps whose
  lease expired, so finished work is never re-executed.
* Delivery is at-least-once for retryable work. Side-effecting tools are made
  idempotent separately (see ``tools.base``).
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from opentelemetry import trace
from sqlalchemy import DateTime, func, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.config import get_settings
from agentos_core.db.models import (
    AgentDefinition,
    ApprovalRequest,
    StepAttempt,
    ToolExecution,
    Worker,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.domain.dag import (
    NodeDefinition,
    NodeType,
    WorkflowSpec,
    _lookup,
    evaluate_condition,
    resolve_refs,
)
from agentos_core.domain.errors import (
    AgentOSError,
    ApprovalRejectedError,
    BudgetExceededError,
    FanOutError,
    SubWorkflowError,
    WorkerLostError,
)
from agentos_core.domain.states import (
    TERMINAL_RUN,
    TERMINAL_STEP,
    ApprovalStatus,
    AttemptStatus,
    RunStatus,
    StepStatus,
    ToolExecutionStatus,
    assert_approval_transition,
    assert_run_transition,
    assert_step_transition,
)
from agentos_core.engine.events import append_event, flush_events
from agentos_core.engine.retry import backoff_seconds, should_retry
from agentos_core.engine.tx import lock_run, notify_work, run_in_transaction
from agentos_core.log import get_logger
from agentos_core.redaction import strip_nul
from agentos_core.util import utcnow

log = get_logger(__name__)
tracer = trace.get_tracer("agentos.engine")

PLANNER_NODE_KEY = "__planner__"


class EngineError(Exception):
    status_code = 409

    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message)
        self.code = code


class NotFoundError(EngineError):
    status_code = 404


# =========================================================================== helpers


async def commit_now(session: AsyncSession) -> None:
    """Commit mid-transaction (before raising), including any buffered events."""
    await flush_events(session)
    await session.commit()


def _set_step_status(step: WorkflowStep, target: StepStatus) -> None:
    assert_step_transition(step.status, target)
    step.status = target.value


def _set_run_status(run: WorkflowRun, target: RunStatus) -> None:
    if run.status == target.value:
        return
    assert_run_transition(run.status, target)
    run.status = target.value


async def _run_steps(session: AsyncSession, run_id: uuid.UUID, *, lock: bool = False) -> list[WorkflowStep]:
    stmt = (
        select(WorkflowStep)
        .where(WorkflowStep.run_id == run_id)
        .order_by(WorkflowStep.created_at, WorkflowStep.node_key)
        .execution_options(populate_existing=True)
    )
    if lock:
        stmt = stmt.with_for_update()
    return list((await session.execute(stmt)).scalars())


async def _attempt_for_token(session: AsyncSession, token: uuid.UUID) -> StepAttempt | None:
    return (
        await session.execute(select(StepAttempt).where(StepAttempt.lease_token == token))
    ).scalar_one_or_none()


def _clear_lease(step: WorkflowStep) -> None:
    step.lease_owner = None
    step.lease_token = None
    step.lease_expires_at = None


def effective_priority(priority: int, waited_seconds: float, aging_seconds: float) -> float:
    """Scheduling score: run priority plus one point per ``aging_seconds`` of run age (starvation protection).

    A priority-0 run that is ``9 * aging_seconds`` older than a newly created priority-9 run outranks it. Runs
    created at the same moment keep their priority gap (they age together); aging protects against later arrivals.
    """
    return priority + max(0.0, waited_seconds) / max(aging_seconds, 1e-6)


def _step_context(run: WorkflowRun, deps: list[WorkflowStep]) -> dict[str, Any]:
    return {"input": run.input, "steps": {d.node_key: {"output": d.output} for d in deps}}


async def _skip_step(session: AsyncSession, run: WorkflowRun, step: WorkflowStep, reason: str) -> None:
    _set_step_status(step, StepStatus.SKIPPED)
    step.completed_at = utcnow()
    await append_event(
        session, run.id, "step.skipped", {"node_key": step.node_key, "reason": reason}, step_id=step.id
    )


async def _fail_engine_step(
    session: AsyncSession, run: WorkflowRun, steps: list[WorkflowStep], step: WorkflowStep, err: AgentOSError
) -> None:
    """Fail an engine-handled step (fan_out / sub_workflow) that never had a worker lease."""
    _set_step_status(step, StepStatus.FAILED)
    step.error = err.to_dict()
    step.completed_at = utcnow()
    await append_event(
        session, run.id, "step.failed", {"node_key": step.node_key, "error": err.to_dict()}, step_id=step.id
    )
    await _halt_unstarted(session, run, steps, reason="upstream_failure")


async def _promote_dependents(session: AsyncSession, run: WorkflowRun, steps: list[WorkflowStep]) -> int:
    """Advance pending steps whose dependencies are all terminal.

    * all deps succeeded (or, with ``trigger: any_succeeded``, at least one) and the ``when`` condition
      holds -> ready; engine-handled nodes (fan_out, sub_workflow) are resolved here instead;
    * a skipped dependency, or a false condition, skips the step (skips propagate downstream);
    * ``waiting_child`` fan_out steps complete once every child is terminal.
    """
    by_key = {s.node_key: s for s in steps}
    now = utcnow()
    promoted = 0
    changed = True
    while changed:
        changed = False
        for s in list(steps):
            if s.status == StepStatus.WAITING_CHILD and s.node_type == NodeType.FAN_OUT:
                changed |= await _finish_fan_out(session, run, by_key, s)
                continue
            if s.status != StepStatus.PENDING:
                continue
            deps = [by_key[d] for d in s.dependency_keys if d in by_key]
            if any(d.status not in (StepStatus.SUCCEEDED, StepStatus.SKIPPED) for d in deps):
                continue
            ok = [d for d in deps if d.status == StepStatus.SUCCEEDED]
            trigger = s.node_config.get("trigger") or "all_succeeded"
            if deps and (len(ok) != len(deps) if trigger == "all_succeeded" else not ok):
                await _skip_step(session, run, s, "upstream_skipped")
                changed = True
                continue
            ctx = _step_context(run, deps)
            cond = s.node_config.get("when")
            if cond and not evaluate_condition(cond, ctx):
                await _skip_step(session, run, s, "condition_false")
                changed = True
                continue
            if s.node_type == NodeType.FAN_OUT:
                await _expand_fan_out(session, run, steps, by_key, s, ctx)
                changed = True
                continue
            if s.node_type == NodeType.SUB_WORKFLOW:
                await _start_sub_workflow(session, run, steps, s, ctx)
                changed = True
                continue
            _set_step_status(s, StepStatus.READY)
            s.ready_at = now
            promoted += 1
            changed = True
            await append_event(session, run.id, "step.ready", {"node_key": s.node_key}, step_id=s.id)
    if promoted:
        notify_work(session)
    return promoted


async def _expand_fan_out(
    session: AsyncSession,
    run: WorkflowRun,
    steps: list[WorkflowStep],
    by_key: dict[str, WorkflowStep],
    step: WorkflowStep,
    ctx: dict[str, Any],
) -> None:
    """Replace a fan_out node by at most ``max_items`` parallel children; the node itself waits for them."""
    from agentos_core.autopilot.versions import resolve_pins

    cfg = step.node_config
    try:
        items = _lookup(cfg["over"], ctx)
    except KeyError:
        return await _fail_engine_step(
            session, run, steps, step, FanOutError(f"fan_out 'over' {cfg['over']!r} did not resolve")
        )
    if not isinstance(items, list):
        return await _fail_engine_step(
            session, run, steps, step, FanOutError(f"fan_out 'over' {cfg['over']!r} is not a list")
        )
    limit = int(cfg.get("max_items") or 10)
    truncated = len(items) > limit
    if truncated and cfg.get("on_overflow") != "truncate":
        return await _fail_engine_step(
            session,
            run,
            steps,
            step,
            FanOutError(
                f"fan_out produced {len(items)} items; max_items is {limit}",
                details={"items": len(items), "max_items": limit},
            ),
        )
    items = items[:limit]
    max_nodes = get_settings().max_dag_nodes
    if len(steps) + len(items) > max_nodes:
        return await _fail_engine_step(
            session,
            run,
            steps,
            step,
            FanOutError(f"expanding {len(items)} children would exceed the {max_nodes}-step run limit"),
        )
    tmpl = cfg["template"]
    agent_id: uuid.UUID | None = None
    version_id: uuid.UUID | None = None
    if tmpl.get("agent"):
        agent_id = (
            await session.execute(
                select(AgentDefinition.id).where(
                    AgentDefinition.workspace_id == run.workspace_id, AgentDefinition.name == tmpl["agent"]
                )
            )
        ).scalar_one_or_none()
        if agent_id is None:
            return await _fail_engine_step(
                session, run, steps, step, FanOutError(f"fan_out template agent {tmpl['agent']!r} not found")
            )
        version_id = next(
            (s.agent_version_id for s in steps if s.agent_definition_id == agent_id and s.agent_version_id),
            None,
        )
        if version_id is None:
            version_id = (await resolve_pins(session, {agent_id}, run.id, None, run.environment)).get(
                agent_id
            )
    children: list[WorkflowStep] = []
    for i, item in enumerate(items):
        key = f"{step.node_key}[{i}]"
        node = NodeDefinition.model_validate({**tmpl, "key": "child", "depends_on": []})
        config = node.model_dump(mode="json", exclude_none=True)
        config.update({"key": key, "fan_out_parent": step.node_key, "fan_out_index": i})
        children.append(
            WorkflowStep(
                id=uuid.uuid4(),
                run_id=run.id,
                node_key=key,
                node_type=node.type.value,
                node_config=config,
                agent_definition_id=agent_id,
                agent_version_id=version_id,
                dependency_keys=list(step.dependency_keys),
                status=StepStatus.PENDING.value,
                input={**(tmpl.get("input") or {}), "item": item, "index": i},
                max_attempts=node.retry.max_attempts,
            )
        )
    session.add_all(children)
    for c in children:
        steps.append(c)
        by_key[c.node_key] = c
    _set_step_status(step, StepStatus.WAITING_CHILD)
    step.started_at = utcnow()
    step.checkpoint = {
        "children": [c.node_key for c in children],
        "count": len(children),
        "truncated": truncated,
    }
    await session.flush()
    await append_event(
        session,
        run.id,
        "step.fan_out",
        {
            "node_key": step.node_key,
            "count": len(children),
            "truncated": truncated,
            "children": [c.node_key for c in children],
        },
        step_id=step.id,
    )


async def _finish_fan_out(
    session: AsyncSession, run: WorkflowRun, by_key: dict[str, WorkflowStep], step: WorkflowStep
) -> bool:
    keys = (step.checkpoint or {}).get("children", [])
    children = [by_key[k] for k in keys if k in by_key]
    if len(children) != len(keys) or not all(
        c.status in (StepStatus.SUCCEEDED, StepStatus.SKIPPED) for c in children
    ):
        return False  # a failed or cancelled child halts the whole run through the normal failure path
    _set_step_status(step, StepStatus.SUCCEEDED)
    step.output = {"results": [c.output for c in children], "count": len(children)}
    step.completed_at = utcnow()
    await append_event(
        session,
        run.id,
        "step.completed",
        {"node_key": step.node_key, "fan_out": len(children)},
        step_id=step.id,
    )
    return True


async def _start_sub_workflow(
    session: AsyncSession,
    run: WorkflowRun,
    steps: list[WorkflowStep],
    step: WorkflowStep,
    ctx: dict[str, Any],
) -> None:
    """Start the latest version of the named workflow as a child run; the step waits for it durably."""
    from agentos_core.workflow_service import latest_workflow, validate_for_workspace

    name = step.node_config["workflow"]
    max_depth = get_settings().max_nested_depth
    if run.depth + 1 > max_depth:
        return await _fail_engine_step(
            session,
            run,
            steps,
            step,
            SubWorkflowError(
                f"nested workflow depth {run.depth + 1} exceeds the limit of {max_depth}",
                details={"reason": "depth_exceeded", "max_depth": max_depth},
            ),
        )
    wf = await latest_workflow(session, run.workspace_id, name)
    if wf is None:
        return await _fail_engine_step(
            session,
            run,
            steps,
            step,
            SubWorkflowError(
                f"workflow {name!r} does not exist in this workspace", details={"reason": "not_found"}
            ),
        )
    result, agents = await validate_for_workspace(session, run.workspace_id, wf.definition)
    if not result.ok or result.spec is None:
        return await _fail_engine_step(
            session,
            run,
            steps,
            step,
            SubWorkflowError(f"workflow {name!r} no longer validates", details={"reason": "invalid"}),
        )
    try:
        child_input = resolve_refs(step.input, ctx)
    except KeyError as exc:
        return await _fail_engine_step(
            session,
            run,
            steps,
            step,
            SubWorkflowError(f"unresolved reference {exc.args[0]!r}", details={"reason": "unresolved_input"}),
        )
    remaining = (
        float(run.budget.get("max_cost_usd", 0)) - float(run.cost_spent_usd) - float(run.cost_reserved_usd)
    )
    if remaining <= 0:
        return await _fail_engine_step(
            session,
            run,
            steps,
            step,
            BudgetExceededError(
                "no budget left to start a nested workflow", details={"limit": "max_cost_usd"}
            ),
        )
    child = await materialize_run(
        session,
        workflow=wf,
        spec=result.spec,
        run_input=child_input,
        requested_by=run.requested_by,
        agents_by_name=agents,
        priority=run.priority,
        parent=(run, step),
        budget_cap_usd=remaining,
        deadline_cap=run.deadline_at,
        environment=run.environment,
    )
    _set_step_status(step, StepStatus.WAITING_CHILD)
    step.started_at = utcnow()
    step.checkpoint = {"child_run_id": str(child.id), "workflow": name, "workflow_version": wf.version}
    await append_event(
        session,
        run.id,
        "step.waiting_child",
        {"node_key": step.node_key, "child_run_id": str(child.id), "workflow": name, "depth": child.depth},
        step_id=step.id,
    )


async def _expire_step_approvals(session: AsyncSession, step: WorkflowStep, reason: str) -> None:
    approvals = (
        await session.execute(
            select(ApprovalRequest).where(
                ApprovalRequest.step_id == step.id, ApprovalRequest.status == ApprovalStatus.PENDING
            )
        )
    ).scalars()
    now = utcnow()
    for a in approvals:
        a.status = ApprovalStatus.EXPIRED.value
        a.decided_at = now
        a.decision_reason = reason
        if a.tool_execution_id:
            await session.execute(
                update(ToolExecution)
                .where(ToolExecution.id == a.tool_execution_id)
                .values(status=ToolExecutionStatus.REJECTED.value, completed_at=now, error={"type": reason})
            )
        await append_event(
            session,
            step.run_id,
            "approval.resolved",
            {"approval_id": str(a.id), "status": "expired", "reason": reason, "node_key": step.node_key},
            step_id=step.id,
        )


def _release_reservation(run: WorkflowRun, attempt: StepAttempt | None) -> None:
    """Give back budget an attempt reserved for a model call it never settled (its worker died or was cancelled).

    Found by the Chaos Lab: every worker crash used to leak the reservation, permanently shrinking the run's budget."""
    if attempt is None or not attempt.reserved_usd:
        return
    run.cost_reserved_usd = max(Decimal(0), (run.cost_reserved_usd or Decimal(0)) - attempt.reserved_usd)
    attempt.reserved_usd = Decimal(0)


async def _cancel_child_run(session: AsyncSession, step: WorkflowStep) -> None:
    """Cancel the child run of a sub_workflow step. Lock order is always parent run, then child run."""
    child_id = (step.checkpoint or {}).get("child_run_id")
    if not child_id:
        return
    with contextlib.suppress(EngineError):  # already terminal
        await cancel_run(session, uuid.UUID(child_id), user_id=None)


async def _halt_unstarted(
    session: AsyncSession, run: WorkflowRun, steps: list[WorkflowStep], *, reason: str
) -> None:
    """After a terminal failure or cancellation, stop everything that has not started."""
    cancel = reason == "cancelled"
    for s in steps:
        if s.status in (StepStatus.PENDING, StepStatus.READY, StepStatus.RETRY_WAIT):
            target = StepStatus.CANCELLED if cancel else StepStatus.SKIPPED
            _set_step_status(s, target)
            s.completed_at = utcnow()
            s.next_run_at = None
            await append_event(
                session,
                run.id,
                f"step.{target.value}",
                {"node_key": s.node_key, "reason": reason},
                step_id=s.id,
            )
        elif s.status == StepStatus.WAITING_CHILD:
            _set_step_status(s, StepStatus.CANCELLED)
            s.completed_at = utcnow()
            if s.node_type == NodeType.SUB_WORKFLOW:
                await _cancel_child_run(session, s)
            await append_event(
                session, run.id, "step.cancelled", {"node_key": s.node_key, "reason": reason}, step_id=s.id
            )
        elif s.status == StepStatus.WAITING_APPROVAL:
            _set_step_status(s, StepStatus.CANCELLED)
            s.completed_at = utcnow()
            await _expire_step_approvals(session, s, reason)
            await append_event(
                session, run.id, "step.cancelled", {"node_key": s.node_key, "reason": reason}, step_id=s.id
            )


def _sink_outputs(steps: list[WorkflowStep]) -> dict[str, Any]:
    # A skipped branch does not consume its upstream output: the upstream step then is a sink.
    depended = {d for s in steps if s.status != StepStatus.SKIPPED for d in s.dependency_keys}
    return {
        s.node_key: s.output
        for s in steps
        if s.node_key not in depended
        and s.node_key != PLANNER_NODE_KEY
        and s.status == StepStatus.SUCCEEDED
        and "fan_out_parent" not in s.node_config  # children are reported through their fan_out node
    }


async def refresh_run(
    session: AsyncSession, run: WorkflowRun, steps: list[WorkflowStep] | None = None
) -> None:
    """Recompute run status from its steps; finalize when every step is terminal."""
    if run.status in TERMINAL_RUN and run.status != RunStatus.CANCELLED:
        return
    steps = steps if steps is not None else await _run_steps(session, run.id)
    statuses = {StepStatus(s.status) for s in steps}
    now = utcnow()

    if run.status == RunStatus.CANCELLED:
        # Cancellation is final immediately; record completion once in-flight steps drain.
        if run.completed_at is None and statuses <= TERMINAL_STEP:
            run.completed_at = now
        return

    if statuses <= TERMINAL_STEP:
        failed = [s for s in steps if s.status == StepStatus.FAILED]
        if failed:
            _set_run_status(run, RunStatus.FAILED)
            run.error = {"failed_steps": [s.node_key for s in failed], "first_error": failed[0].error}
            event = "run.failed"
        else:
            _set_run_status(run, RunStatus.SUCCEEDED)
            run.output = _sink_outputs(steps)
            event = "run.completed"
        run.completed_at = now
        await append_event(
            session,
            run.id,
            event,
            {"status": run.status, "error": run.error, "duration_ms": _ms(run.started_at, now)},
        )
        return

    if run.status == RunStatus.PLANNING:
        return
    waiting = StepStatus.WAITING_APPROVAL in statuses
    active = statuses & {
        StepStatus.RUNNING,
        StepStatus.READY,
        StepStatus.RETRY_WAIT,
        StepStatus.WAITING_CHILD,
    }
    if waiting and not active and run.status != RunStatus.WAITING_APPROVAL:
        _set_run_status(run, RunStatus.WAITING_APPROVAL)
        await append_event(session, run.id, "run.waiting_approval", {})
    elif run.status == RunStatus.WAITING_APPROVAL and (active or not waiting):
        _set_run_status(run, RunStatus.RUNNING)
        await append_event(session, run.id, "run.resumed", {})


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return int((end - start).total_seconds() * 1000)


# =========================================================================== run creation


@dataclass
class AgentRef:
    id: uuid.UUID
    name: str


async def materialize_run(
    session: AsyncSession,
    *,
    workflow: WorkflowDefinition,
    spec: WorkflowSpec,
    run_input: dict[str, Any],
    requested_by: uuid.UUID | None,
    agents_by_name: dict[str, AgentRef],
    planned_task: str | None = None,
    version_overrides: dict[uuid.UUID, uuid.UUID] | None = None,
    purpose: str = "workflow",
    priority: int = 0,
    parent: tuple[WorkflowRun, WorkflowStep] | None = None,
    budget_cap_usd: float | None = None,
    deadline_cap: datetime | None = None,
    replay_of_run_id: uuid.UUID | None = None,
    chaos_experiment_id: uuid.UUID | None = None,
    chaos_case_index: int | None = None,
    budget_override: dict[str, Any] | None = None,
    environment: str = "production",
) -> WorkflowRun:
    """Create a run and its steps atomically. Root steps become ready immediately.

    Every agent step is pinned to an agent config version here (override > canary > active),
    so later edits, promotions or rollbacks never change a run that already started.
    """
    now = utcnow()
    run_id = uuid.uuid4()
    # The run's root span: a child of the HTTP request (or of the parent run's step) when there is one. Its context is
    # stored on the run so worker processes can continue the same trace.
    with tracer.start_as_current_span(
        "run.create",
        attributes={
            "agentos.run_id": str(run_id),
            "agentos.workflow": workflow.name,
            "agentos.environment": environment,
            "agentos.purpose": purpose,
        },
    ) as root:
        root_ctx = root.get_span_context()
    traceparent = (
        f"00-{format(root_ctx.trace_id, '032x')}-{format(root_ctx.span_id, '016x')}-01"
        if root_ctx.is_valid
        else None
    )
    budget = {**spec.budget.model_dump(), **(budget_override or {})}
    if budget_cap_usd is not None:
        budget["max_cost_usd"] = min(float(budget["max_cost_usd"]), budget_cap_usd)
    deadline = now + timedelta(seconds=spec.budget.max_duration_seconds)
    if deadline_cap is not None:
        deadline = min(deadline, deadline_cap)
    run = WorkflowRun(
        id=run_id,
        trace_id=format(root_ctx.trace_id, "032x") if root_ctx.is_valid else None,
        traceparent=traceparent,
        workspace_id=workflow.workspace_id,
        workflow_definition_id=workflow.id,
        workflow_version=workflow.version,
        status=RunStatus.PENDING.value,
        input=run_input,
        requested_by=requested_by,
        budget=budget,
        deadline_at=deadline,
        event_seq=0,
        purpose=purpose,
        priority=priority,
        parent_run_id=parent[0].id if parent else None,
        parent_step_id=parent[1].id if parent else None,
        depth=(parent[0].depth + 1) if parent else 0,
        replay_of_run_id=replay_of_run_id,
        chaos_experiment_id=chaos_experiment_id,
        chaos_case_index=chaos_case_index,
        environment=environment,
    )
    session.add(run)
    await session.flush()
    await append_event(
        session, run.id, "run.created", {"workflow": workflow.name, "version": workflow.version}
    )

    await _add_steps(session, run.id, spec, agents_by_name, version_overrides, environment)
    steps = await _run_steps(session, run.id)
    _set_run_status(run, RunStatus.RUNNING)
    run.started_at = now
    await append_event(session, run.id, "run.started", {"step_count": len(steps)})
    await _promote_dependents(session, run, steps)
    # Engine-handled root nodes (fan_out, sub_workflow) and skipped branches can finish a run right here.
    await refresh_run(session, run, steps)
    return run


async def _add_steps(
    session: AsyncSession,
    run_id: uuid.UUID,
    spec: WorkflowSpec,
    agents_by_name: dict[str, AgentRef],
    version_overrides: dict[uuid.UUID, uuid.UUID] | None,
    environment: str = "production",
) -> None:
    from agentos_core.autopilot.versions import resolve_pins, version_retry_overrides

    agent_ids = {agents_by_name[n.agent].id for n in spec.nodes if n.agent}
    pins = await resolve_pins(session, agent_ids, run_id, version_overrides, environment)
    retry = await version_retry_overrides(session, {v for v in pins.values() if v})
    for node in spec.nodes:
        agent_id = agents_by_name[node.agent].id if node.agent else None
        version_id = pins.get(agent_id) if agent_id else None
        session.add(
            WorkflowStep(
                id=uuid.uuid4(),
                run_id=run_id,
                node_key=node.key,
                node_type=node.type.value,
                node_config=node.model_dump(mode="json", exclude_none=True),
                agent_definition_id=agent_id,
                agent_version_id=version_id,
                dependency_keys=list(node.depends_on),
                status=StepStatus.PENDING.value,
                input=node.input,
                # An agent version may carry its own retry policy (an optimizable setting).
                max_attempts=retry.get(version_id, node.retry.max_attempts)
                if version_id
                else node.retry.max_attempts,
            )
        )
    await session.flush()


async def create_planning_run(
    session: AsyncSession,
    *,
    workflow: WorkflowDefinition,
    task: str,
    run_input: dict[str, Any],
    requested_by: uuid.UUID | None,
    budget: dict[str, Any],
) -> WorkflowRun:
    """Create a run whose DAG is produced by the planner step at execution time."""
    now = utcnow()
    run = WorkflowRun(
        id=uuid.uuid4(),
        workspace_id=workflow.workspace_id,
        workflow_definition_id=workflow.id,
        workflow_version=workflow.version,
        status=RunStatus.PENDING.value,
        input={**run_input, "task": task},
        requested_by=requested_by,
        budget=budget,
        deadline_at=now + timedelta(seconds=int(budget.get("max_duration_seconds", 3600))),
    )
    session.add(run)
    await session.flush()
    await append_event(session, run.id, "run.created", {"workflow": workflow.name, "mode": "planned"})
    planner = WorkflowStep(
        id=uuid.uuid4(),
        run_id=run.id,
        node_key=PLANNER_NODE_KEY,
        node_type="plan",
        node_config={"key": PLANNER_NODE_KEY, "type": "plan", "task": task, "timeout_seconds": 300},
        dependency_keys=[],
        status=StepStatus.READY.value,
        ready_at=now,
        input={"task": task},
        max_attempts=3,
    )
    session.add(planner)
    _set_run_status(run, RunStatus.PLANNING)
    run.started_at = now
    await session.flush()
    await append_event(session, run.id, "step.ready", {"node_key": PLANNER_NODE_KEY}, step_id=planner.id)
    notify_work(session)
    return run


# =========================================================================== claiming


@dataclass
class ClaimedStep:
    step_id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    node_key: str
    node_type: str
    node_config: dict[str, Any]
    step_input: dict[str, Any]
    run_input: dict[str, Any]
    budget: dict[str, Any]
    checkpoint: dict[str, Any] | None
    agent_definition_id: uuid.UUID | None
    agent_version_id: uuid.UUID | None
    run_purpose: str
    replay_of_run_id: uuid.UUID | None
    chaos: dict[str, Any] | None
    dependency_outputs: dict[str, Any]
    attempt_id: uuid.UUID
    attempt_number: int
    lease_token: uuid.UUID
    lease_expires_at: datetime
    max_attempts: int
    failure_count: int
    traceparent: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


async def claim_steps(worker_id: str, limit: int, *, lease_ttl: float | None = None) -> list[ClaimedStep]:
    if limit <= 0:
        return []
    ttl = lease_ttl if lease_ttl is not None else get_settings().lease_ttl_seconds

    async def _claim(session: AsyncSession) -> list[ClaimedStep]:
        now = utcnow()
        # Runs are ordered by effective priority = run priority + run age / aging interval. Age is measured from run
        # creation, not from when the next step became ready: a step-level clock restarts every time a multi-step
        # run advances, which a benchmark showed starves low-priority runs once per step (see AGENTOS_2_BENCHMARKS).
        aging = get_settings().priority_aging_seconds
        oldest = func.min(WorkflowStep.ready_at)
        score = (
            WorkflowRun.priority
            + func.extract("epoch", literal(now, DateTime(timezone=True)) - WorkflowRun.created_at) / aging
        )
        runnable_runs = (
            await session.execute(
                select(WorkflowStep.run_id, oldest.label("oldest"))
                .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
                .where(
                    WorkflowStep.status == StepStatus.READY,
                    WorkflowRun.paused_at.is_(None),
                    WorkflowRun.status.in_(
                        [RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.PLANNING]
                    ),
                )
                .group_by(WorkflowStep.run_id, WorkflowRun.priority, WorkflowRun.created_at)
                .order_by(score.desc(), oldest)
                .limit(max(limit * 4, 16))
            )
        ).all()
        claimed: list[ClaimedStep] = []
        for run_id, _ in runnable_runs:
            if len(claimed) >= limit:
                break
            run = await lock_run(session, run_id, skip_locked=True)
            if run is None or run.status in TERMINAL_RUN or run.paused_at is not None:
                continue  # mutated, paused or finished since the ordering query; try it next tick
            running = (
                await session.execute(
                    select(func.count()).where(
                        WorkflowStep.run_id == run_id, WorkflowStep.status == StepStatus.RUNNING
                    )
                )
            ).scalar_one()
            slots = min(limit - len(claimed), int(run.budget.get("max_concurrency", 4)) - running)
            if slots <= 0:
                continue
            steps = list(
                (
                    await session.execute(
                        select(WorkflowStep)
                        .where(WorkflowStep.run_id == run_id, WorkflowStep.status == StepStatus.READY)
                        .order_by(WorkflowStep.ready_at, WorkflowStep.node_key)
                        .limit(slots)
                        .with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True)
                    )
                ).scalars()
            )
            for step in steps:
                claimed.append(await _claim_one(session, run, step, worker_id, ttl, now))
        return claimed

    return await run_in_transaction(_claim)


async def _claim_one(
    session: AsyncSession, run: WorkflowRun, step: WorkflowStep, worker_id: str, ttl: float, now: datetime
) -> ClaimedStep:
    _set_step_status(step, StepStatus.RUNNING)
    token = uuid.uuid4()
    step.lease_owner = worker_id
    step.lease_token = token
    step.lease_expires_at = now + timedelta(seconds=ttl)
    step.attempt_count += 1
    step.started_at = step.started_at or now
    if step.retry_deadline_at is None:
        deadline = (step.node_config.get("retry") or {}).get("deadline_seconds")
        if deadline:
            step.retry_deadline_at = now + timedelta(seconds=float(deadline))
    queue_wait = _ms(step.ready_at, now)
    attempt = StepAttempt(
        id=uuid.uuid4(),
        step_id=step.id,
        run_id=run.id,
        attempt_number=step.attempt_count,
        worker_id=worker_id,
        lease_token=token,
        status=AttemptStatus.RUNNING.value,
        started_at=now,
        queue_wait_ms=queue_wait,
        execution_metadata={"resumed": step.checkpoint is not None},
    )
    session.add(attempt)
    deps: dict[str, Any] = {}
    if step.dependency_keys:
        rows = await session.execute(
            select(WorkflowStep.node_key, WorkflowStep.output).where(
                WorkflowStep.run_id == run.id, WorkflowStep.node_key.in_(step.dependency_keys)
            )
        )
        outputs = {k: o for k, o in rows.all()}
        # Declared order, not row order: the prompt built from these must be identical on every execution
        # (the unordered SELECT made it vary, which a deterministic re-execution exposed).
        deps = {k: outputs[k] for k in step.dependency_keys if k in outputs}
    await append_event(
        session,
        run.id,
        "step.started",
        {
            "node_key": step.node_key,
            "attempt": step.attempt_count,
            "worker_id": worker_id,
            "queue_wait_ms": queue_wait,
            "resumed": step.checkpoint is not None,
        },
        step_id=step.id,
    )
    await session.flush()
    return ClaimedStep(
        step_id=step.id,
        run_id=run.id,
        workspace_id=run.workspace_id,
        node_key=step.node_key,
        node_type=step.node_type,
        node_config=step.node_config,
        step_input=step.input,
        run_input=run.input,
        budget=run.budget,
        checkpoint=step.checkpoint,
        agent_definition_id=step.agent_definition_id,
        agent_version_id=step.agent_version_id,
        run_purpose=run.purpose,
        replay_of_run_id=run.replay_of_run_id,
        chaos=await _chaos_for(session, run),
        dependency_outputs=deps,
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        lease_token=token,
        lease_expires_at=step.lease_expires_at or now,
        max_attempts=step.max_attempts,
        failure_count=step.failure_count,
        traceparent=run.traceparent,
    )


async def _chaos_for(session: AsyncSession, run: WorkflowRun) -> dict[str, Any] | None:
    """The fault plan for a run that belongs to a running chaos experiment (None for every normal run)."""
    if run.chaos_experiment_id is None:
        return None
    from agentos_core.db.models import ChaosExperiment

    exp = await session.get(ChaosExperiment, run.chaos_experiment_id)
    if exp is None or exp.status != "running" or exp.started_at is None:
        return None
    ends = exp.started_at + timedelta(seconds=int(exp.plan.get("duration_seconds", 120)))
    return {
        "experiment_id": str(exp.id),
        "case_index": run.chaos_case_index or 0,
        "plan": exp.plan,
        "ends_at": ends.isoformat(),
    }


# =========================================================================== fenced results


class StaleLeaseError(EngineError):
    """The caller no longer owns the step; its result was discarded."""

    def __init__(self) -> None:
        super().__init__("lease is no longer valid; result discarded", code="stale_lease")


async def _fenced_step(
    session: AsyncSession, step_id: uuid.UUID, token: uuid.UUID, run_id: uuid.UUID | None = None
) -> tuple[WorkflowRun, WorkflowStep, StepAttempt | None]:
    """Lock run then step and verify the lease. Raises StaleLeaseError on mismatch."""
    if run_id is None:
        run_id = (
            await session.execute(select(WorkflowStep.run_id).where(WorkflowStep.id == step_id))
        ).scalar_one()
    run = await lock_run(session, run_id)
    assert run is not None
    step = (
        await session.execute(
            select(WorkflowStep)
            .where(WorkflowStep.id == step_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    attempt = await _attempt_for_token(session, token)
    if step.status != StepStatus.RUNNING or step.lease_token != token:
        if attempt is not None and attempt.status == AttemptStatus.RUNNING:
            attempt.status = AttemptStatus.STALE.value
            attempt.finished_at = utcnow()
        elif attempt is not None and attempt.status == AttemptStatus.LEASE_EXPIRED:
            attempt.execution_metadata = {
                **attempt.execution_metadata,
                "late_result_rejected_at": utcnow().isoformat(),
            }
        await append_event(
            session,
            run.id,
            "step.stale_result_rejected",
            {"node_key": step.node_key, "attempt": attempt.attempt_number if attempt else None},
            step_id=step.id,
        )
        raise StaleLeaseError()
    return run, step, attempt


async def complete_step(
    step_id: uuid.UUID,
    token: uuid.UUID,
    output: dict[str, Any],
    *,
    metadata: dict[str, Any] | None = None,
    run_id: uuid.UUID | None = None,
) -> None:
    async def _do(session: AsyncSession) -> None:
        try:
            run, step, attempt = await _fenced_step(session, step_id, token, run_id)
        except StaleLeaseError:
            # Persist the stale-attempt bookkeeping, then surface the rejection.
            await commit_now(session)  # persist stale-attempt bookkeeping and its events
            raise
        now = utcnow()
        if attempt:
            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.finished_at = now
            attempt.execution_metadata = {**attempt.execution_metadata, **(metadata or {})}
            _release_reservation(run, attempt)
        step.output = strip_nul(output)
        step.checkpoint = None
        step.completed_at = now
        step.error = None
        _clear_lease(step)
        if run.status == RunStatus.CANCELLED:
            _set_step_status(step, StepStatus.CANCELLED)
            await append_event(
                session,
                run.id,
                "step.cancelled",
                {"node_key": step.node_key, "reason": "completed_after_cancellation"},
                step_id=step.id,
            )
        else:
            _set_step_status(step, StepStatus.SUCCEEDED)
            await append_event(
                session,
                run.id,
                "step.completed",
                {
                    "node_key": step.node_key,
                    "attempt": attempt.attempt_number if attempt else None,
                    "duration_ms": _ms(attempt.started_at if attempt else None, now),
                },
                step_id=step.id,
            )
        await session.flush()
        steps = await _run_steps(session, run.id)
        if run.status != RunStatus.CANCELLED:
            await _promote_dependents(session, run, steps)
        await refresh_run(session, run, steps)

    await run_in_transaction(_do)


async def fail_step(
    step_id: uuid.UUID, token: uuid.UUID, error: AgentOSError, *, run_id: uuid.UUID | None = None
) -> StepStatus:
    """Record a failed attempt; schedule a retry or fail the step (and the run) permanently."""
    result: dict[str, StepStatus] = {}

    async def _do(session: AsyncSession) -> None:
        try:
            run, step, attempt = await _fenced_step(session, step_id, token, run_id)
        except StaleLeaseError:
            await commit_now(session)  # persist stale-attempt bookkeeping and its events
            raise
        result["status"] = await _record_failure(
            session, run, step, attempt, error, attempt_status=AttemptStatus.FAILED
        )

    await run_in_transaction(_do)
    return result["status"]


async def _record_failure(
    session: AsyncSession,
    run: WorkflowRun,
    step: WorkflowStep,
    attempt: StepAttempt | None,
    error: AgentOSError,
    *,
    attempt_status: AttemptStatus,
) -> StepStatus:
    now = utcnow()
    if attempt:
        attempt.status = attempt_status.value
        attempt.finished_at = now
        _release_reservation(run, attempt)
        attempt.error_type = error.error_type
        attempt.error_message = strip_nul(error.message[:4000])
    step.failure_count += 1
    step.error = strip_nul(error.to_dict())
    _clear_lease(step)

    if run.status == RunStatus.CANCELLED:
        _set_step_status(step, StepStatus.CANCELLED)
        step.completed_at = now
        await append_event(session, run.id, "step.cancelled", {"node_key": step.node_key}, step_id=step.id)
        await refresh_run(session, run)
        return StepStatus.CANCELLED

    retry_cfg = step.node_config.get("retry") or {}
    delay = backoff_seconds(
        step.failure_count,
        base=float(retry_cfg.get("backoff_base_seconds", 1.0)),
        cap=float(retry_cfg.get("backoff_max_seconds", 30.0)),
        jitter=bool(retry_cfg.get("jitter", True)),
    )
    if should_retry(
        error,
        failure_count=step.failure_count,
        max_attempts=step.max_attempts,
        now=now,
        retry_deadline_at=step.retry_deadline_at,
        next_delay=delay,
    ):
        _set_step_status(step, StepStatus.RETRY_WAIT)
        step.next_run_at = now + timedelta(seconds=delay)
        await append_event(
            session,
            run.id,
            "step.retrying",
            {
                "node_key": step.node_key,
                "failure_count": step.failure_count,
                "max_attempts": step.max_attempts,
                "delay_seconds": round(delay, 3),
                "error": error.to_dict(),
            },
            step_id=step.id,
        )
        return StepStatus.RETRY_WAIT

    _set_step_status(step, StepStatus.FAILED)
    step.completed_at = now
    await append_event(
        session,
        run.id,
        "step.failed",
        {"node_key": step.node_key, "error": error.to_dict(), "failure_count": step.failure_count},
        step_id=step.id,
    )
    await session.flush()
    steps = await _run_steps(session, run.id)
    await _halt_unstarted(session, run, steps, reason="upstream_failure")
    await refresh_run(session, run, steps)
    return StepStatus.FAILED


async def checkpoint_step(step_id: uuid.UUID, token: uuid.UUID, checkpoint: dict[str, Any]) -> None:
    """Persist agent progress mid-step (fenced) so a crash resumes instead of restarting."""

    async def _do(session: AsyncSession) -> None:
        res = await session.execute(
            update(WorkflowStep)
            .where(
                WorkflowStep.id == step_id,
                WorkflowStep.lease_token == token,
                WorkflowStep.status == StepStatus.RUNNING,
            )
            .values(checkpoint=strip_nul(checkpoint))
        )
        if res.rowcount == 0:  # type: ignore[attr-defined]
            raise StaleLeaseError()

    await run_in_transaction(_do)


# =========================================================================== approvals


@dataclass
class ApprovalSpec:
    requested_action: str
    reason: str | None
    affected_resource: str | None
    requesting_agent: str | None
    sanitized_arguments: dict[str, Any]
    arguments_hash: str
    tool_execution_id: uuid.UUID | None = None


async def suspend_for_approval(
    step_id: uuid.UUID, token: uuid.UUID, *, checkpoint: dict[str, Any] | None, approval: ApprovalSpec
) -> uuid.UUID:
    """Atomically pause a step and open an approval request. Returns the approval id."""
    out: dict[str, uuid.UUID] = {}

    async def _do(session: AsyncSession) -> None:
        try:
            run, step, attempt = await _fenced_step(session, step_id, token)
        except StaleLeaseError:
            await commit_now(session)  # persist stale-attempt bookkeeping and its events
            raise
        now = utcnow()
        existing = (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.step_id == step.id, ApprovalRequest.status == ApprovalStatus.PENDING
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            approval_row = existing  # duplicate request collapses onto the open one
        else:
            approval_row = ApprovalRequest(
                id=uuid.uuid4(),
                workspace_id=run.workspace_id,
                run_id=run.id,
                step_id=step.id,
                tool_execution_id=approval.tool_execution_id,
                requested_action=approval.requested_action,
                reason=approval.reason,
                affected_resource=approval.affected_resource,
                requesting_agent=approval.requesting_agent,
                sanitized_arguments=approval.sanitized_arguments,
                arguments_hash=approval.arguments_hash,
                status=ApprovalStatus.PENDING.value,
                requested_at=now,
                expires_at=now + timedelta(seconds=get_settings().approval_ttl_seconds),
            )
            session.add(approval_row)
        if attempt:
            attempt.status = AttemptStatus.SUSPENDED.value
            attempt.finished_at = now
        step.checkpoint = strip_nul(checkpoint)
        _clear_lease(step)
        _set_step_status(step, StepStatus.WAITING_APPROVAL)
        await session.flush()
        await append_event(
            session,
            run.id,
            "approval.requested",
            {
                "approval_id": str(approval_row.id),
                "node_key": step.node_key,
                "action": approval.requested_action,
                "resource": approval.affected_resource,
                "agent": approval.requesting_agent,
                "arguments": approval.sanitized_arguments,
                "expires_at": approval_row.expires_at.isoformat(),
            },
            step_id=step.id,
        )
        await append_event(session, run.id, "step.suspended", {"node_key": step.node_key}, step_id=step.id)
        await refresh_run(session, run)
        out["id"] = approval_row.id

    await run_in_transaction(_do)
    return out["id"]


async def resolve_approval(
    session: AsyncSession,
    approval_id: uuid.UUID,
    *,
    decision: ApprovalStatus,
    user_id: uuid.UUID | None,
    reason: str | None,
) -> ApprovalRequest:
    """Approve, reject or expire. Single-use: only a pending, unexpired request can be decided.

    The decision happens in its own trace (an HTTP request or the coordinator); a span link ties it to the run's trace."""
    from agentos_core.telemetry import link_to

    traceparent = (
        await session.execute(
            select(WorkflowRun.traceparent)
            .join(ApprovalRequest, ApprovalRequest.run_id == WorkflowRun.id)
            .where(ApprovalRequest.id == approval_id)
        )
    ).scalar_one_or_none()
    with tracer.start_as_current_span(
        "approval.resolve",
        links=link_to(traceparent),
        attributes={"agentos.approval_id": str(approval_id), "agentos.decision": decision.value},
    ):
        return await _resolve_approval(
            session, approval_id, decision=decision, user_id=user_id, reason=reason
        )


async def _resolve_approval(
    session: AsyncSession,
    approval_id: uuid.UUID,
    *,
    decision: ApprovalStatus,
    user_id: uuid.UUID | None,
    reason: str | None,
) -> ApprovalRequest:
    run_id = (
        await session.execute(select(ApprovalRequest.run_id).where(ApprovalRequest.id == approval_id))
    ).scalar_one_or_none()
    if run_id is None:
        raise NotFoundError("approval not found", code="not_found")
    run = await lock_run(session, run_id)
    assert run is not None
    approval = (
        await session.execute(
            select(ApprovalRequest)
            .where(ApprovalRequest.id == approval_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    now = utcnow()
    if approval.status != ApprovalStatus.PENDING:
        raise EngineError(f"approval already {approval.status}", code="approval_not_pending")
    if decision != ApprovalStatus.EXPIRED and approval.expires_at <= now:
        decision, reason = ApprovalStatus.EXPIRED, "expired_before_decision"
    assert_approval_transition(approval.status, decision)
    approval.status = decision.value
    approval.decided_at = now
    approval.decided_by = user_id if decision != ApprovalStatus.EXPIRED else None
    approval.decision_reason = reason

    step = (
        await session.execute(
            select(WorkflowStep)
            .where(WorkflowStep.id == approval.step_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    if approval.tool_execution_id:
        te_status = (
            ToolExecutionStatus.APPROVED
            if decision == ApprovalStatus.APPROVED
            else ToolExecutionStatus.REJECTED
        )
        await session.execute(
            update(ToolExecution)
            .where(ToolExecution.id == approval.tool_execution_id)
            .values(
                status=te_status.value,
                completed_at=None if te_status == ToolExecutionStatus.APPROVED else now,
            )
        )
    await append_event(
        session,
        run.id,
        "approval.resolved",
        {
            "approval_id": str(approval.id),
            "status": decision.value,
            "node_key": step.node_key,
            "decided_by": str(user_id) if user_id else None,
            "reason": reason,
        },
        step_id=step.id,
    )

    if step.status != StepStatus.WAITING_APPROVAL or run.status in TERMINAL_RUN:
        return approval

    if step.node_type == NodeType.APPROVAL_GATE:
        if decision == ApprovalStatus.APPROVED:
            _set_step_status(step, StepStatus.SUCCEEDED)
            step.output = {"approved": True, "approval_id": str(approval.id), "decided_by": str(user_id)}
            step.completed_at = now
            approval.consumed_at = now
            await append_event(
                session, run.id, "step.completed", {"node_key": step.node_key}, step_id=step.id
            )
            steps = await _run_steps(session, run.id)
            await _promote_dependents(session, run, steps)
            await refresh_run(session, run, steps)
        else:
            err = ApprovalRejectedError(f"approval {decision.value}: {reason or 'no reason given'}")
            step.failure_count += 1
            step.error = err.to_dict()
            _set_step_status(step, StepStatus.FAILED)
            step.completed_at = now
            await append_event(
                session,
                run.id,
                "step.failed",
                {"node_key": step.node_key, "error": err.to_dict()},
                step_id=step.id,
            )
            steps = await _run_steps(session, run.id)
            await _halt_unstarted(session, run, steps, reason="upstream_failure")
            await refresh_run(session, run, steps)
        return approval

    # Agent / tool steps resume; the executor applies the decision from the checkpoint.
    _set_step_status(step, StepStatus.READY)
    step.ready_at = now
    await append_event(
        session, run.id, "step.ready", {"node_key": step.node_key, "resumed": True}, step_id=step.id
    )
    await refresh_run(session, run)
    notify_work(session)
    return approval


async def consume_approval(
    session: AsyncSession, approval_id: uuid.UUID, *, tool_execution_id: uuid.UUID, arguments_hash: str
) -> bool:
    """Mark an approval used. Fails if it was already used or the arguments changed."""
    res = await session.execute(
        update(ApprovalRequest)
        .where(
            ApprovalRequest.id == approval_id,
            ApprovalRequest.status == ApprovalStatus.APPROVED,
            ApprovalRequest.consumed_at.is_(None),
            ApprovalRequest.tool_execution_id == tool_execution_id,
            ApprovalRequest.arguments_hash == arguments_hash,
        )
        .values(consumed_at=utcnow())
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]


# =========================================================================== recovery (coordinator)


async def expire_leases(*, now: datetime | None = None, batch: int = 50) -> int:
    """Reclaim running steps whose worker stopped heartbeating."""
    now = now or utcnow()
    async with _read_session() as session:
        rows = (
            await session.execute(
                select(WorkflowStep.id, WorkflowStep.run_id)
                .where(WorkflowStep.status == StepStatus.RUNNING, WorkflowStep.lease_expires_at < now)
                .limit(batch)
            )
        ).all()
    count = 0
    for step_id, run_id in rows:

        async def _do(
            session: AsyncSession, step_id: uuid.UUID = step_id, run_id: uuid.UUID = run_id
        ) -> bool:
            run = await lock_run(session, run_id, skip_locked=True)
            if run is None:
                return False
            step = (
                await session.execute(
                    select(WorkflowStep)
                    .where(WorkflowStep.id == step_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalar_one()
            if (
                step.status != StepStatus.RUNNING
                or step.lease_expires_at is None
                or step.lease_expires_at >= utcnow()
            ):
                return False
            attempt = await _attempt_for_token(session, step.lease_token) if step.lease_token else None
            err = WorkerLostError(
                f"lease held by {step.lease_owner} expired at {step.lease_expires_at.isoformat()}",
                details={"worker_id": step.lease_owner},
            )
            await _record_failure(
                session, run, step, attempt, err, attempt_status=AttemptStatus.LEASE_EXPIRED
            )
            log.warning("lease_expired", run_id=str(run_id), node_key=step.node_key, worker=step.lease_owner)
            return True

        if await run_in_transaction(_do):
            count += 1
    return count


async def promote_retries(*, now: datetime | None = None, batch: int = 100) -> int:
    now = now or utcnow()
    async with _read_session() as session:
        run_ids = (
            (
                await session.execute(
                    select(WorkflowStep.run_id)
                    .where(WorkflowStep.status == StepStatus.RETRY_WAIT, WorkflowStep.next_run_at <= now)
                    .distinct()
                    .limit(batch)
                )
            )
            .scalars()
            .all()
        )
    total = 0
    for run_id in run_ids:

        async def _do(session: AsyncSession, run_id: uuid.UUID = run_id) -> int:
            run = await lock_run(session, run_id, skip_locked=True)
            if run is None:
                return 0
            steps = (
                await session.execute(
                    select(WorkflowStep)
                    .where(
                        WorkflowStep.run_id == run_id,
                        WorkflowStep.status == StepStatus.RETRY_WAIT,
                        WorkflowStep.next_run_at <= utcnow(),
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).scalars()
            n = 0
            for s in steps:
                _set_step_status(s, StepStatus.READY)
                s.ready_at = utcnow()
                s.next_run_at = None
                n += 1
                await append_event(
                    session, run_id, "step.ready", {"node_key": s.node_key, "retry": True}, step_id=s.id
                )
            if n:
                notify_work(session)
                await refresh_run(session, run)
            return n

        total += await run_in_transaction(_do)
    return total


async def expire_approvals(*, now: datetime | None = None) -> int:
    now = now or utcnow()
    async with _read_session() as session:
        ids = (
            (
                await session.execute(
                    select(ApprovalRequest.id).where(
                        ApprovalRequest.status == ApprovalStatus.PENDING, ApprovalRequest.expires_at <= now
                    )
                )
            )
            .scalars()
            .all()
        )
    for approval_id in ids:

        async def _do(session: AsyncSession, approval_id: uuid.UUID = approval_id) -> None:
            with contextlib.suppress(EngineError):  # decided concurrently
                await resolve_approval(
                    session,
                    approval_id,
                    decision=ApprovalStatus.EXPIRED,
                    user_id=None,
                    reason="approval_ttl_elapsed",
                )

        await run_in_transaction(_do)
    return len(ids)


async def enforce_deadlines(*, now: datetime | None = None) -> int:
    now = now or utcnow()
    async with _read_session() as session:
        ids = (
            (
                await session.execute(
                    select(WorkflowRun.id).where(
                        WorkflowRun.status.in_(
                            [RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.PLANNING]
                        ),
                        WorkflowRun.deadline_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
    for run_id in ids:

        async def _do(session: AsyncSession, run_id: uuid.UUID = run_id) -> None:
            run = await lock_run(session, run_id, skip_locked=True)
            if run is None or run.status in TERMINAL_RUN:
                return
            steps = await _run_steps(session, run_id, lock=True)
            err = BudgetExceededError(
                "run exceeded max_duration_seconds", details={"limit": "max_duration_seconds"}
            )
            for s in steps:
                if s.status == StepStatus.RUNNING:
                    attempt = await _attempt_for_token(session, s.lease_token) if s.lease_token else None
                    if attempt:
                        attempt.status = AttemptStatus.CANCELLED.value
                        attempt.finished_at = utcnow()
                        _release_reservation(run, attempt)
                    _clear_lease(s)
                    _set_step_status(s, StepStatus.FAILED)
                    s.error = err.to_dict()
                    s.completed_at = utcnow()
                    await append_event(
                        session,
                        run_id,
                        "step.failed",
                        {"node_key": s.node_key, "error": err.to_dict()},
                        step_id=s.id,
                    )
            await append_event(session, run_id, "budget.exceeded", {"limit": "max_duration_seconds"})
            await _halt_unstarted(session, run, steps, reason="run_deadline_exceeded")
            await refresh_run(session, run, steps)

        await run_in_transaction(_do)
    return len(ids)


# =========================================================================== pause / resume


async def pause_run(
    session: AsyncSession, run_id: uuid.UUID, *, user_id: uuid.UUID | None, reason: str | None = None
) -> WorkflowRun:
    """Stop claiming new steps for a run (and its child runs). Running steps finish; state is kept."""
    run = await lock_run(session, run_id)
    if run is None:
        raise NotFoundError("run not found", code="not_found")
    if run.status in TERMINAL_RUN:
        raise EngineError(f"run already {run.status}", code="run_terminal")
    if run.paused_at is None:
        run.paused_at = utcnow()
        run.pause_reason = reason
        await append_event(
            session, run_id, "run.paused", {"by": str(user_id) if user_id else None, "reason": reason}
        )
    for child_id in await _child_run_ids(session, run_id):  # parent lock first, then child
        with contextlib.suppress(EngineError):
            await pause_run(session, child_id, user_id=user_id, reason=reason)
    return run


async def resume_run(session: AsyncSession, run_id: uuid.UUID, *, user_id: uuid.UUID | None) -> WorkflowRun:
    run = await lock_run(session, run_id)
    if run is None:
        raise NotFoundError("run not found", code="not_found")
    if run.status in TERMINAL_RUN:
        raise EngineError(f"run already {run.status}", code="run_terminal")
    if run.paused_at is not None:
        waited = _ms(run.paused_at, utcnow())
        run.paused_at = None
        run.pause_reason = None
        await append_event(
            session, run_id, "run.unpaused", {"by": str(user_id) if user_id else None, "paused_ms": waited}
        )
        notify_work(session)
    for child_id in await _child_run_ids(session, run_id):
        with contextlib.suppress(EngineError):
            await resume_run(session, child_id, user_id=user_id)
    return run


async def _child_run_ids(session: AsyncSession, run_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        (
            await session.execute(
                select(WorkflowRun.id)
                .where(WorkflowRun.parent_run_id == run_id)
                .order_by(WorkflowRun.created_at)
            )
        ).scalars()
    )


# =========================================================================== nested workflows


async def subflow_tick(*, batch: int = 50) -> int:
    """Propagate finished child runs to the sub_workflow steps waiting for them.

    Done by the coordinator (not inside the child's own transaction) so lock order stays parent -> child
    and a crash between the two commits is harmless: the waiting step is simply picked up on the next tick.
    """
    async with _read_session() as session:
        rows = (
            await session.execute(
                select(WorkflowStep.id, WorkflowStep.run_id, WorkflowRun.id)
                .join(WorkflowRun, WorkflowRun.parent_step_id == WorkflowStep.id)
                .where(
                    WorkflowStep.status == StepStatus.WAITING_CHILD,
                    WorkflowStep.node_type == NodeType.SUB_WORKFLOW,
                    WorkflowRun.status.in_([s.value for s in TERMINAL_RUN]),
                )
                .limit(batch)
            )
        ).all()
    done = 0
    for step_id, parent_id, child_id in rows:

        async def _do(
            session: AsyncSession,
            step_id: uuid.UUID = step_id,
            parent_id: uuid.UUID = parent_id,
            child_id: uuid.UUID = child_id,
        ) -> bool:
            run = await lock_run(session, parent_id, skip_locked=True)
            if run is None or run.status in TERMINAL_RUN:
                return False
            steps = await _run_steps(session, parent_id, lock=True)
            step = next((x for x in steps if x.id == step_id), None)
            child = await session.get(WorkflowRun, child_id)
            if step is None or child is None or step.status != StepStatus.WAITING_CHILD:
                return False
            now = utcnow()
            run.cost_spent_usd = (run.cost_spent_usd or Decimal(0)) + (child.cost_spent_usd or Decimal(0))
            run.model_calls += child.model_calls
            run.tool_calls += child.tool_calls
            if child.status == RunStatus.SUCCEEDED:
                _set_step_status(step, StepStatus.SUCCEEDED)
                step.output = {"child_run_id": str(child.id), "output": child.output}
                step.completed_at = now
                await append_event(
                    session,
                    parent_id,
                    "step.completed",
                    {"node_key": step.node_key, "child_run_id": str(child.id)},
                    step_id=step.id,
                )
                await session.flush()
                await _promote_dependents(session, run, steps)
            else:
                err = SubWorkflowError(
                    f"child run ended {child.status}",
                    details={
                        "reason": "child_failed",
                        "child_run_id": str(child.id),
                        "child_status": child.status,
                        "child_error": child.error,
                    },
                )
                await _fail_engine_step(session, run, steps, step, err)
            await refresh_run(session, run, steps)
            return True

        if await run_in_transaction(_do):
            done += 1
    return done


# =========================================================================== cancellation


async def cancel_run(session: AsyncSession, run_id: uuid.UUID, *, user_id: uuid.UUID | None) -> WorkflowRun:
    run = await lock_run(session, run_id)
    if run is None:
        raise NotFoundError("run not found", code="not_found")
    if run.status in TERMINAL_RUN:
        raise EngineError(f"run already {run.status}", code="run_terminal")
    _set_run_status(run, RunStatus.CANCELLED)
    run.cancel_requested_at = utcnow()
    steps = await _run_steps(session, run_id, lock=True)
    await _halt_unstarted(session, run, steps, reason="cancelled")
    in_flight = [s.node_key for s in steps if s.status == StepStatus.RUNNING]
    await append_event(
        session,
        run_id,
        "run.cancelled",
        {"cancelled_by": str(user_id) if user_id else None, "in_flight_steps": in_flight},
    )
    if not in_flight:
        run.completed_at = utcnow()
    from agentos_core.engine.tx import queue_notify
    from agentos_core.notify import CANCEL_CHANNEL

    queue_notify(session, CANCEL_CHANNEL, {"run_id": str(run_id)})
    return run


async def mark_step_cancelled(step_id: uuid.UUID, token: uuid.UUID, *, note: str) -> None:
    """Worker acknowledges cooperative cancellation of its in-flight step."""

    async def _do(session: AsyncSession) -> None:
        try:
            run, step, attempt = await _fenced_step(session, step_id, token)
        except StaleLeaseError:
            await commit_now(session)  # persist stale-attempt bookkeeping and its events
            raise
        now = utcnow()
        if attempt:
            attempt.status = AttemptStatus.CANCELLED.value
            attempt.finished_at = now
            attempt.error_message = note
            _release_reservation(run, attempt)
        _clear_lease(step)
        _set_step_status(step, StepStatus.CANCELLED)
        step.completed_at = now
        await append_event(
            session, run.id, "step.cancelled", {"node_key": step.node_key, "note": note}, step_id=step.id
        )
        await refresh_run(session, run)

    await run_in_transaction(_do)


# =========================================================================== heartbeats & leases


@dataclass
class HeartbeatResult:
    renewed: set[uuid.UUID]
    lost: set[uuid.UUID]
    cancelled: set[uuid.UUID]


async def heartbeat(
    worker_id: str,
    tokens: list[uuid.UUID],
    *,
    hostname: str,
    pid: int,
    concurrency: int,
    lease_ttl: float,
    steps_completed: int | None = None,
) -> HeartbeatResult:
    async def _do(session: AsyncSession) -> HeartbeatResult:
        now = utcnow()
        from sqlalchemy.dialects.postgresql import insert

        await session.execute(
            insert(Worker)
            .values(
                id=worker_id,
                hostname=hostname,
                pid=pid,
                concurrency=concurrency,
                status="active",
                in_flight=len(tokens),
                started_at=now,
                last_heartbeat_at=now,
                steps_completed=0,
            )
            .on_conflict_do_update(
                index_elements=[Worker.id],
                set_={
                    "last_heartbeat_at": now,
                    "in_flight": len(tokens),
                    "status": "active",
                    # Reported by the worker so completions never touch this shared row.
                    **({"steps_completed": steps_completed} if steps_completed is not None else {}),
                },
            )
        )
        renewed: set[uuid.UUID] = set()
        cancelled: set[uuid.UUID] = set()
        if tokens:
            rows = (
                await session.execute(
                    update(WorkflowStep)
                    .where(
                        WorkflowStep.lease_token.in_(tokens),
                        WorkflowStep.status == StepStatus.RUNNING,
                        WorkflowStep.lease_owner == worker_id,
                    )
                    .values(lease_expires_at=now + timedelta(seconds=lease_ttl))
                    .returning(WorkflowStep.lease_token, WorkflowStep.run_id)
                )
            ).all()
            renewed = {r[0] for r in rows if r[0] is not None}
            run_ids = {r[1] for r in rows}
            if run_ids:
                dead = set(
                    (
                        await session.execute(
                            select(WorkflowRun.id).where(
                                WorkflowRun.id.in_(run_ids), WorkflowRun.status.in_(list(TERMINAL_RUN))
                            )
                        )
                    ).scalars()
                )
                cancelled = {tok for tok, rid in rows if rid in dead and tok is not None}
        return HeartbeatResult(renewed=renewed, lost=set(tokens) - renewed, cancelled=cancelled)

    return await run_in_transaction(_do)


async def lease_valid(session: AsyncSession, step_id: uuid.UUID, token: uuid.UUID) -> bool:
    row = (
        await session.execute(
            select(WorkflowStep.id).where(
                WorkflowStep.id == step_id,
                WorkflowStep.lease_token == token,
                WorkflowStep.status == StepStatus.RUNNING,
                WorkflowStep.lease_expires_at > func.now(),
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def mark_worker_stopped(worker_id: str) -> None:
    async def _do(session: AsyncSession) -> None:
        await session.execute(
            update(Worker).where(Worker.id == worker_id).values(status="stopped", in_flight=0)
        )

    await run_in_transaction(_do)


# =========================================================================== budgets


async def reserve_model_call(
    run_id: uuid.UUID, estimate_usd: Decimal, attempt_id: uuid.UUID | None = None
) -> None:
    """Atomically reserve budget for one model call or raise BudgetExceededError.

    The reservation is also recorded on the attempt, so it can be released if that attempt dies mid-call."""

    async def _do(session: AsyncSession) -> str | None:
        from agentos_core.finops.budgets import check_budgets

        denial = await check_budgets(session, run_id, attempt_id, estimate_usd)
        if denial is not None:
            await lock_run(session, run_id)
            await append_event(
                session,
                run_id,
                "budget.exceeded",
                {
                    "limit": denial.limit_name,
                    "budget_id": str(denial.budget_id),
                    "period": denial.period,
                    "spent_usd": float(denial.spent),
                    "reserved_usd": float(denial.in_flight),
                    "limit_usd": float(denial.limit),
                },
            )
            return f"{denial.scope} budget exhausted ({denial.period} limit ${denial.limit})"
        res = await session.execute(
            update(WorkflowRun)
            .where(
                WorkflowRun.id == run_id,
                WorkflowRun.model_calls
                < func.coalesce(WorkflowRun.budget["max_model_calls"].as_integer(), 50),
                WorkflowRun.cost_spent_usd + WorkflowRun.cost_reserved_usd + estimate_usd
                <= func.coalesce(WorkflowRun.budget["max_cost_usd"].as_numeric(14, 8), Decimal("5")),
            )
            .values(
                model_calls=WorkflowRun.model_calls + 1,
                cost_reserved_usd=WorkflowRun.cost_reserved_usd + estimate_usd,
            )
        )
        if res.rowcount:  # type: ignore[attr-defined]
            if attempt_id is not None:
                await session.execute(
                    update(StepAttempt)
                    .where(StepAttempt.id == attempt_id)
                    .values(reserved_usd=StepAttempt.reserved_usd + estimate_usd)
                )
            return None
        await lock_run(session, run_id)
        await append_event(session, run_id, "budget.exceeded", {"limit": "model_calls_or_cost"})
        return "run budget exhausted (max_model_calls or max_cost_usd)"

    problem = await run_in_transaction(_do)
    if problem is not None:
        raise BudgetExceededError(problem)


async def settle_model_call(
    session: AsyncSession,
    run_id: uuid.UUID,
    reserved: Decimal,
    actual: Decimal | None,
    attempt_id: uuid.UUID | None = None,
) -> None:
    """Release a reservation and charge actual (or, if unknown, the reserved) cost."""
    if attempt_id is not None:
        await session.execute(
            update(StepAttempt)
            .where(StepAttempt.id == attempt_id)
            .values(reserved_usd=func.greatest(StepAttempt.reserved_usd - reserved, 0))
        )
    charge = actual if actual is not None else reserved
    await session.execute(
        update(WorkflowRun)
        .where(WorkflowRun.id == run_id)
        .values(
            cost_reserved_usd=func.greatest(WorkflowRun.cost_reserved_usd - reserved, 0),
            cost_spent_usd=WorkflowRun.cost_spent_usd + charge,
        )
    )


async def reserve_tool_call(session: AsyncSession, run_id: uuid.UUID) -> bool:
    res = await session.execute(
        update(WorkflowRun)
        .where(
            WorkflowRun.id == run_id,
            WorkflowRun.tool_calls < func.coalesce(WorkflowRun.budget["max_tool_calls"].as_integer(), 100),
        )
        .values(tool_calls=WorkflowRun.tool_calls + 1)
    )
    return bool(res.rowcount)  # type: ignore[attr-defined]


# =========================================================================== misc


def _read_session() -> Any:
    from agentos_core.db.session import get_sessionmaker

    return get_sessionmaker()()


async def load_agent(session: AsyncSession, agent_id: uuid.UUID) -> AgentDefinition:
    return (await session.execute(select(AgentDefinition).where(AgentDefinition.id == agent_id))).scalar_one()


__all__ = [
    "PLANNER_NODE_KEY",
    "AgentRef",
    "ApprovalSpec",
    "ClaimedStep",
    "EngineError",
    "HeartbeatResult",
    "NotFoundError",
    "StaleLeaseError",
    "cancel_run",
    "checkpoint_step",
    "claim_steps",
    "complete_step",
    "consume_approval",
    "create_planning_run",
    "enforce_deadlines",
    "expire_approvals",
    "expire_leases",
    "fail_step",
    "heartbeat",
    "lease_valid",
    "materialize_run",
    "promote_retries",
    "refresh_run",
    "reserve_model_call",
    "reserve_tool_call",
    "resolve_approval",
    "settle_model_call",
    "suspend_for_approval",
]


# =========================================================================== planning & usage


async def complete_planner_step(
    step_id: uuid.UUID,
    token: uuid.UUID,
    *,
    spec: WorkflowSpec,
    agents_by_name: dict[str, AgentRef],
    plan_meta: dict[str, Any],
) -> None:
    """Fenced: materialize a validated plan into steps and move the run planning -> running."""

    async def _do(session: AsyncSession) -> None:
        try:
            run, step, attempt = await _fenced_step(session, step_id, token)
        except StaleLeaseError:
            await commit_now(session)  # persist stale-attempt bookkeeping and its events
            raise
        now = utcnow()
        plan = spec.model_dump(mode="json", exclude_none=True)
        if attempt:
            attempt.status = AttemptStatus.SUCCEEDED.value
            attempt.finished_at = now
        step.output = {"plan": plan, **plan_meta}
        step.checkpoint = None
        step.completed_at = now
        _clear_lease(step)
        _set_step_status(step, StepStatus.SUCCEEDED)
        await append_event(session, run.id, "step.completed", {"node_key": step.node_key}, step_id=step.id)
        if run.status == RunStatus.CANCELLED:
            await refresh_run(session, run)
            return
        # A plan may tighten the run budget but never loosen it.
        planned = spec.budget.model_dump()
        run.budget = {k: min(planned[k], run.budget.get(k, planned[k])) for k in planned}
        await _add_steps(session, run.id, spec, agents_by_name, None, run.environment)
        _set_run_status(run, RunStatus.RUNNING)
        await append_event(
            session, run.id, "run.planned", {"node_count": len(spec.nodes), "plan": plan, **plan_meta}
        )
        steps = [s for s in await _run_steps(session, run.id) if s.node_key != PLANNER_NODE_KEY]
        await _promote_dependents(session, run, steps)
        await refresh_run(session, run)

    await run_in_transaction(_do)


@dataclass
class UsageRecord:
    provider: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cached_input_tokens: int | None
    cache_write_tokens: int | None
    latency_ms: int
    estimated_cost_usd: Decimal | None
    cost_basis: str
    pricing_version: str
    finish_reason: str | None
    status: str = "succeeded"
    error_type: str | None = None


async def record_model_call(
    *,
    run_id: uuid.UUID,
    step_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None,
    agent_definition_id: uuid.UUID | None,
    usage: UsageRecord,
    reserved_usd: Decimal,
    event_payload: dict[str, Any],
    body: Any = None,
    node_key: str | None = None,
    attempt_number: int | None = None,
    agent_version_id: uuid.UUID | None = None,
) -> None:
    """Persist usage, settle the budget reservation, append the event and (optionally) the sanitized
    request/response record, all in one transaction."""
    from agentos_core.db.models import ModelCallRecord, ModelUsage

    async def _do(session: AsyncSession) -> None:
        run = await lock_run(session, run_id)
        if body is not None and run is not None:
            index = (
                await session.execute(
                    select(func.count())
                    .select_from(ModelCallRecord)
                    .where(ModelCallRecord.step_id == step_id)
                )
            ).scalar_one()
            session.add(
                ModelCallRecord(
                    id=body.record_id,
                    workspace_id=run.workspace_id,
                    run_id=run_id,
                    step_id=step_id,
                    attempt_id=attempt_id,
                    node_key=node_key or "",
                    call_index=index,
                    attempt_number=attempt_number,
                    provider=usage.provider,
                    model=usage.model,
                    agent_version_id=agent_version_id,
                    request_digest=body.request_digest,
                    request=body.request,
                    response=body.response,
                    error=body.error,
                    status=usage.status,
                    latency_ms=usage.latency_ms,
                    truncated=body.truncated,
                )
            )
        session.add(
            ModelUsage(
                run_id=run_id,
                step_id=step_id,
                attempt_id=attempt_id,
                agent_definition_id=agent_definition_id,
                provider=usage.provider,
                model=usage.model,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_input_tokens=usage.cached_input_tokens,
                cache_write_tokens=usage.cache_write_tokens,
                latency_ms=usage.latency_ms,
                estimated_cost_usd=usage.estimated_cost_usd,
                cost_basis=usage.cost_basis,
                pricing_version=usage.pricing_version,
                finish_reason=usage.finish_reason,
                status=usage.status,
                error_type=usage.error_type,
                recorded_at=utcnow(),
            )
        )
        actual = usage.estimated_cost_usd if usage.status == "succeeded" else Decimal(0)
        await settle_model_call(session, run_id, reserved_usd, actual, attempt_id)
        await append_event(
            session,
            run_id,
            "model.call.completed" if usage.status == "succeeded" else "model.call.failed",
            event_payload,
            step_id=step_id,
        )

    await run_in_transaction(_do)


async def emit_event(
    run_id: uuid.UUID, event_type: str, payload: dict[str, Any], *, step_id: uuid.UUID | None = None
) -> None:
    async def _do(session: AsyncSession) -> None:
        await lock_run(session, run_id)
        await append_event(session, run_id, event_type, payload, step_id=step_id)

    await run_in_transaction(_do)
