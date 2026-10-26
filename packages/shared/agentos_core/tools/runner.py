"""Policy-enforcing tool execution.

The order of checks is fixed and enforced in code (never by prompt):

1. tool is registered and in the step's allowed set (agent ∩ node policy);
2. arguments validate against the tool's schema;
3. the run's tool-call budget has room;
4. approval-gated tools need an approved, unconsumed approval bound to the exact
   arguments hash and tool execution id;
5. side-effecting tools are deduplicated by idempotency key;
6. execution under a timeout, with redacted arguments and results persisted.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from opentelemetry import trace
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import ApprovalRequest, ToolExecution
from agentos_core.domain.errors import AgentOSError, LeaseLostError, classify
from agentos_core.domain.states import ToolExecutionStatus
from agentos_core.engine.core import ApprovalSpec, consume_approval, reserve_tool_call
from agentos_core.engine.events import append_event
from agentos_core.engine.tx import lock_run, run_in_transaction
from agentos_core.redaction import redact
from agentos_core.storage import Storage
from agentos_core.tools.base import SideEffect, ToolContext
from agentos_core.tools.registry import ToolRegistry
from agentos_core.util import canonical_json, sha256_hex, utcnow

tracer = trace.get_tracer("agentos.tools")


@dataclass
class ToolCallRequest:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class StepScope:
    workspace_id: uuid.UUID
    run_id: uuid.UUID
    step_id: uuid.UUID
    node_key: str
    attempt_id: uuid.UUID
    lease_token: uuid.UUID
    agent_name: str | None
    allowed_tools: frozenset[str]
    storage: Storage


@dataclass
class ToolOutcome:
    kind: Literal["result", "error", "needs_approval", "denied"]
    content: dict[str, Any]
    tool_execution_id: uuid.UUID | None = None
    approval: ApprovalSpec | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.kind in ("error", "denied")


def arguments_hash(name: str, arguments: dict[str, Any]) -> str:
    return sha256_hex(f"{name}:{canonical_json(arguments)}")


def idempotency_key(scope: StepScope, name: str, args_hash: str) -> str:
    return sha256_hex(f"{scope.run_id}:{scope.node_key}:{name}:{args_hash}")


async def _event(session: AsyncSession, scope: StepScope, event_type: str, payload: dict[str, Any]) -> None:
    await lock_run(session, scope.run_id)
    await append_event(
        session, scope.run_id, event_type, {"node_key": scope.node_key, **payload}, step_id=scope.step_id
    )


async def _record(
    scope: StepScope,
    call: ToolCallRequest,
    *,
    status: ToolExecutionStatus,
    args_hash: str,
    idem: str,
    requires_approval: bool,
    error: dict[str, Any] | None = None,
    event: str | None = None,
    reason: str | None = None,
) -> uuid.UUID:
    te_id = uuid.uuid4()

    async def _do(session: AsyncSession) -> None:
        await lock_run(session, scope.run_id)
        session.add(
            ToolExecution(
                id=te_id,
                run_id=scope.run_id,
                step_id=scope.step_id,
                attempt_id=scope.attempt_id,
                tool_call_id=call.call_id,
                tool_name=call.name,
                arguments=redact(call.arguments),
                arguments_hash=args_hash,
                status=status.value,
                idempotency_key=idem,
                requires_approval=requires_approval,
                error=error,
                reason=reason,
                completed_at=utcnow()
                if status in (ToolExecutionStatus.DENIED, ToolExecutionStatus.FAILED)
                else None,
            )
        )
        if event:
            await _event(
                session, scope, event, {"tool": call.name, "tool_execution_id": str(te_id), "error": error}
            )

    await run_in_transaction(_do)
    return te_id


async def execute_tool_call(call: ToolCallRequest, scope: StepScope, registry: ToolRegistry) -> ToolOutcome:
    with tracer.start_as_current_span(
        "tool.call", attributes={"agentos.run_id": str(scope.run_id), "agentos.tool": call.name}
    ) as span:
        outcome = await _execute(call, scope, registry)
        span.set_attribute("agentos.tool.outcome", outcome.kind)
        return outcome


async def _execute(call: ToolCallRequest, scope: StepScope, registry: ToolRegistry) -> ToolOutcome:
    args_hash = arguments_hash(call.name, call.arguments)
    idem = idempotency_key(scope, call.name, args_hash)
    tool = registry.get(call.name)

    # 1. authorization
    if tool is None or call.name not in scope.allowed_tools:
        err = {"type": "tool_not_allowed", "message": f"tool {call.name!r} is not permitted for this step"}
        te = await _record(
            scope,
            call,
            status=ToolExecutionStatus.DENIED,
            args_hash=args_hash,
            idem=idem,
            requires_approval=False,
            error=err,
            event="tool.denied",
        )
        return ToolOutcome("denied", {"error": err["message"]}, te)

    # 2. schema validation
    try:
        args = tool.args_model.model_validate(call.arguments)
    except ValidationError as exc:
        msg = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors(include_url=False)
        )
        err = {"type": "invalid_tool_arguments", "message": msg}
        te = await _record(
            scope,
            call,
            status=ToolExecutionStatus.FAILED,
            args_hash=args_hash,
            idem=idem,
            requires_approval=tool.requires_approval,
            error=err,
            event="tool.failed",
        )
        return ToolOutcome("error", {"error": f"invalid arguments: {msg}"}, te)

    # 5 (early). previously succeeded side effect with identical arguments -> return cached result
    if tool.side_effect != SideEffect.NONE:
        cached = await _succeeded_execution(idem)
        if cached is not None:
            return ToolOutcome("result", {**(cached.result or {}), "idempotent_replay": True}, cached.id)

    # 4. approval gate
    approved_te: ToolExecution | None = None
    if tool.requires_approval:
        prior = await _execution_for_call(scope, call.call_id, args_hash)
        if prior is None or prior.status == ToolExecutionStatus.PENDING_APPROVAL:
            action, resource = tool.describe_action(args)
            reason = call.arguments.get("reason") if isinstance(call.arguments.get("reason"), str) else None
            te_id = (
                prior.id
                if prior
                else await _record(
                    scope,
                    call,
                    status=ToolExecutionStatus.PENDING_APPROVAL,
                    args_hash=args_hash,
                    idem=idem,
                    requires_approval=True,
                    reason=reason,
                )
            )
            spec = ApprovalSpec(
                requested_action=action,
                reason=reason,
                affected_resource=resource,
                requesting_agent=scope.agent_name,
                sanitized_arguments=_sanitize_for_review(call.arguments),
                arguments_hash=args_hash,
                tool_execution_id=te_id,
            )
            return ToolOutcome("needs_approval", {}, te_id, approval=spec)
        if prior.status == ToolExecutionStatus.REJECTED:
            reason = await _decision_reason(prior.id)
            return ToolOutcome(
                "error",
                {
                    "error": "A human reviewer did not approve this action, so it was NOT executed.",
                    "decision": reason or "rejected",
                },
                prior.id,
                extra={"rejected": True},
            )
        if prior.status != ToolExecutionStatus.APPROVED:
            return ToolOutcome("error", {"error": f"tool execution is {prior.status}"}, prior.id)
        approved_te = prior

    # 3. budget
    async def _reserve(session: AsyncSession) -> bool:
        return await reserve_tool_call(session, scope.run_id)

    if not await run_in_transaction(_reserve):
        err = {"type": "budget_exceeded", "message": "run tool-call budget exhausted"}
        te = await _record(
            scope,
            call,
            status=ToolExecutionStatus.DENIED,
            args_hash=args_hash,
            idem=idem,
            requires_approval=tool.requires_approval,
            error=err,
            event="tool.denied",
        )
        return ToolOutcome("denied", {"error": err["message"]}, te, extra={"budget_exceeded": True})

    # mark running (consuming the approval atomically when gated)
    if approved_te is not None:
        te_id = approved_te.id

        async def _start_approved(session: AsyncSession) -> bool:
            await lock_run(session, scope.run_id)
            approval_id = (
                await session.execute(
                    select(ApprovalRequest.id).where(ApprovalRequest.tool_execution_id == te_id)
                )
            ).scalar_one_or_none()
            if approval_id is None or not await consume_approval(
                session, approval_id, tool_execution_id=te_id, arguments_hash=args_hash
            ):
                return False
            await session.execute(
                update(ToolExecution)
                .where(ToolExecution.id == te_id)
                .values(
                    status=ToolExecutionStatus.RUNNING.value, started_at=utcnow(), attempt_id=scope.attempt_id
                )
            )
            await _event(session, scope, "tool.started", {"tool": call.name, "tool_execution_id": str(te_id)})
            return True

        if not await run_in_transaction(_start_approved):
            return ToolOutcome(
                "error", {"error": "approval is not valid for these arguments or was already used"}, te_id
            )
    else:
        te_id = uuid.uuid4()

        async def _start(session: AsyncSession) -> None:
            await lock_run(session, scope.run_id)
            session.add(
                ToolExecution(
                    id=te_id,
                    run_id=scope.run_id,
                    step_id=scope.step_id,
                    attempt_id=scope.attempt_id,
                    tool_call_id=call.call_id,
                    tool_name=call.name,
                    arguments=redact(call.arguments),
                    arguments_hash=args_hash,
                    status=ToolExecutionStatus.RUNNING.value,
                    idempotency_key=idem,
                    requires_approval=False,
                    started_at=utcnow(),
                )
            )
            await _event(session, scope, "tool.started", {"tool": call.name, "tool_execution_id": str(te_id)})

        await run_in_transaction(_start)

    # 6. execute
    ctx = ToolContext(
        workspace_id=scope.workspace_id,
        run_id=scope.run_id,
        step_id=scope.step_id,
        node_key=scope.node_key,
        attempt_id=scope.attempt_id,
        lease_token=scope.lease_token,
        tool_execution_id=te_id,
        idempotency_key=idem,
        storage=scope.storage,
    )
    try:
        result = await asyncio.wait_for(tool.run(args, ctx), timeout=tool.timeout_seconds)
    except LeaseLostError:
        await _finish(
            scope,
            te_id,
            call.name,
            status=ToolExecutionStatus.FAILED,
            error={"type": "lease_lost", "message": "lease lost before side effect"},
        )
        raise
    except (AgentOSError, TimeoutError, ValueError, OSError) as exc:
        err = classify(exc).to_dict()
        await _finish(scope, te_id, call.name, status=ToolExecutionStatus.FAILED, error=err)
        return ToolOutcome("error", {"error": err["message"]}, te_id)
    await _finish(scope, te_id, call.name, status=ToolExecutionStatus.SUCCEEDED, result=result)
    return ToolOutcome("result", result, te_id)


async def _finish(
    scope: StepScope,
    te_id: uuid.UUID,
    name: str,
    *,
    status: ToolExecutionStatus,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    async def _do(session: AsyncSession) -> None:
        await lock_run(session, scope.run_id)
        await session.execute(
            update(ToolExecution)
            .where(ToolExecution.id == te_id)
            .values(
                status=status.value,
                result=redact(result) if result is not None else None,
                error=error,
                completed_at=utcnow(),
            )
        )
        await _event(
            session,
            scope,
            "tool.completed" if status == ToolExecutionStatus.SUCCEEDED else "tool.failed",
            {
                "tool": name,
                "tool_execution_id": str(te_id),
                "error": error,
                "summary": _summarize(result) if result else None,
            },
        )

    await run_in_transaction(_do)


async def _succeeded_execution(idem: str) -> ToolExecution | None:
    from agentos_core.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(ToolExecution).where(
                    ToolExecution.idempotency_key == idem,
                    ToolExecution.status == ToolExecutionStatus.SUCCEEDED,
                )
            )
        ).scalar_one_or_none()


async def _execution_for_call(scope: StepScope, call_id: str, args_hash: str) -> ToolExecution | None:
    from agentos_core.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(ToolExecution)
                .where(
                    ToolExecution.step_id == scope.step_id,
                    ToolExecution.tool_call_id == call_id,
                    ToolExecution.arguments_hash == args_hash,
                )
                .order_by(ToolExecution.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def _decision_reason(te_id: uuid.UUID) -> str | None:
    from agentos_core.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(ApprovalRequest.status, ApprovalRequest.decision_reason).where(
                    ApprovalRequest.tool_execution_id == te_id
                )
            )
        ).first()
    if row is None:
        return None
    return f"{row[0]}: {row[1]}" if row[1] else str(row[0])


def _sanitize_for_review(arguments: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = redact(arguments)
    content = clean.get("content")
    if isinstance(content, str) and len(content) > 4000:
        clean["content"] = content[:4000] + f"\n… [{len(content) - 4000} more characters]"
    return clean


def _summarize(result: dict[str, Any]) -> dict[str, Any]:
    keys = ("result", "result_count", "artifact_id", "filename", "deduplicated", "format")
    return {k: result[k] for k in keys if k in result}
