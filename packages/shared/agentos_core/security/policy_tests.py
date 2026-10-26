"""Engine-level enforcement tests: each case drives the real tool-execution path and checks what the code did.

Nothing is mocked: the cases call ``execute_tool_call`` / the approval functions against PostgreSQL and then read the
persisted records (tool executions, approvals, artifacts) as evidence. A case *passes* when the platform refused the
action, and it must also show that the action was actually attempted (otherwise it would pass vacuously).
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select

from agentos_core.db.models import (
    ApprovalRequest,
    Artifact,
    Document,
    StepAttempt,
    ToolExecution,
    User,
    WorkflowDefinition,
    WorkflowStep,
    Workspace,
)
from agentos_core.documents import ingest_document
from agentos_core.domain.dag import WorkflowSpec
from agentos_core.domain.states import ApprovalStatus
from agentos_core.engine import core
from agentos_core.engine.core import materialize_run
from agentos_core.engine.tx import unit_of_work
from agentos_core.security.corpus import CANARY
from agentos_core.storage import Storage
from agentos_core.tools.registry import get_registry
from agentos_core.tools.runner import StepScope, ToolCallRequest, execute_tool_call
from agentos_core.util import utcnow

SCOPE_WORKFLOW = "security-scope"


@dataclass
class CaseOutcome:
    verdict: str  # pass | fail | error
    observed: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyCase:
    key: str
    category: str
    title: str
    expected: str
    run: Any  # async (env) -> CaseOutcome


class Env:
    """Synthetic runs whose single step is already RUNNING under a lease held by the suite (and the run is paused so
    no worker claims anything), giving each case a real step scope to call the real tool-execution path with."""

    def __init__(self, workspace_id: uuid.UUID, user_id: uuid.UUID | None, storage: Storage) -> None:
        self.workspace_id, self.user_id, self.storage = workspace_id, user_id, storage
        self.probe_workspace_id: uuid.UUID | None = None
        self.runs: list[uuid.UUID] = []

    @staticmethod
    def _lease(step: WorkflowStep, run_id: uuid.UUID) -> StepAttempt:
        step.status = "running"
        step.lease_owner = "security-suite"
        step.lease_token = uuid.uuid4()
        step.lease_expires_at = utcnow() + timedelta(minutes=10)
        step.attempt_count += 1
        step.started_at = step.started_at or utcnow()
        return StepAttempt(
            step_id=step.id,
            run_id=run_id,
            attempt_number=step.attempt_count,
            worker_id="security-suite",
            lease_token=step.lease_token,
            status="running",
            started_at=utcnow(),
        )

    async def scope(self, allowed: set[str], *, max_tool_calls: int | None = None) -> StepScope:
        async with unit_of_work() as s:
            wf = (
                await s.execute(
                    select(WorkflowDefinition).where(
                        WorkflowDefinition.workspace_id == self.workspace_id,
                        WorkflowDefinition.name == SCOPE_WORKFLOW,
                    )
                )
            ).scalar_one_or_none()
            definition = {"nodes": [{"key": "probe", "type": "aggregation"}]}
            if wf is None:
                wf = WorkflowDefinition(
                    workspace_id=self.workspace_id, name=SCOPE_WORKFLOW, version=1, definition=definition
                )
                s.add(wf)
                await s.flush()
            run = await materialize_run(
                s,
                workflow=wf,
                spec=WorkflowSpec.model_validate(definition),
                run_input={},
                requested_by=self.user_id,
                agents_by_name={},
                purpose="security",
                budget_override={"max_tool_calls": max_tool_calls} if max_tool_calls is not None else None,
            )
            await core.pause_run(s, run.id, user_id=None, reason="security scope: never executed by a worker")
            step = (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id == run.id))).scalar_one()
            attempt = self._lease(step, run.id)
            s.add(attempt)
            await s.flush()
            self.runs.append(run.id)
            return self._to_scope(run.id, step, attempt, allowed)

    def _to_scope(
        self, run_id: uuid.UUID, step: WorkflowStep, attempt: StepAttempt, allowed: set[str] | frozenset[str]
    ) -> StepScope:
        return StepScope(
            workspace_id=self.workspace_id,
            run_id=run_id,
            step_id=step.id,
            node_key="probe",
            attempt_id=attempt.id,
            lease_token=attempt.lease_token,
            agent_name="Security Probe",
            allowed_tools=frozenset(allowed),
            storage=self.storage,
        )

    async def reclaim(self, scope: StepScope) -> StepScope:
        """What a worker does when it resumes a step after an approval: a new attempt under a new lease."""
        async with unit_of_work() as s:
            step = (
                await s.execute(
                    select(WorkflowStep).where(WorkflowStep.id == scope.step_id).with_for_update()
                )
            ).scalar_one()
            if step.status != "ready":
                raise RuntimeError(f"step is {step.status}, not ready to resume")
            attempt = self._lease(step, scope.run_id)
            s.add(attempt)
            await s.flush()
            return self._to_scope(scope.run_id, step, attempt, scope.allowed_tools)

    async def request_approval(self, scope: StepScope, call: ToolCallRequest) -> Any:
        """Drive the real flow up to a pending human decision: the call needs approval, the step suspends, and the
        approval request is opened (bound to the call's exact arguments)."""
        out = await execute_tool_call(call, scope, get_registry())
        if out.kind != "needs_approval" or out.approval is None:
            raise RuntimeError(f"expected needs_approval, got {out.kind}")
        await core.suspend_for_approval(
            scope.step_id, scope.lease_token, checkpoint={"security": True}, approval=out.approval
        )
        async with unit_of_work() as s:
            return (
                await s.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == scope.run_id))
            ).scalar_one()

    async def probe_workspace(self) -> uuid.UUID:
        """An ephemeral second workspace holding a canary document and data file, owned by nobody who can log in."""
        if self.probe_workspace_id is not None:
            return self.probe_workspace_id
        async with unit_of_work() as s:
            owner = await s.get(User, self.user_id) if self.user_id else None
            ws = Workspace(
                id=uuid.uuid4(),
                name=f"__security_probe__{uuid.uuid4().hex[:6]}",
                owner_id=owner.id if owner else None,
            )
            s.add(ws)
            await s.flush()
            await ingest_document(
                s,
                workspace_id=ws.id,
                filename="canary-notes.md",
                data=f"# Quarterly Blue Heron\n\nQuarterly Blue Heron report. The secret marker is {CANARY}.".encode(),
                storage=self.storage,
            )
            await ingest_document(
                s,
                workspace_id=ws.id,
                filename="other-workspace-secret.json",
                data=f'{{"marker": "{CANARY}"}}'.encode(),
                storage=self.storage,
            )
            self.probe_workspace_id = ws.id
        return ws.id

    async def cleanup(self) -> None:
        # The approved-write cases really write artifacts; they are test data, so they are removed again.
        async with unit_of_work() as s:
            rows = (
                (await s.execute(select(Artifact).where(Artifact.run_id.in_(self.runs)))).scalars().all()
                if self.runs
                else []
            )
            for a in rows:
                if a.storage_path:
                    with contextlib.suppress(OSError):
                        self.storage.resolve(a.storage_path).unlink()
                await s.delete(a)
        for run_id in self.runs:
            async with unit_of_work() as s:
                with contextlib.suppress(core.EngineError):
                    await core.cancel_run(s, run_id, user_id=None)
        if self.probe_workspace_id is not None:
            async with unit_of_work() as s:
                ws = await s.get(Workspace, self.probe_workspace_id)
                if ws is not None:
                    await s.delete(ws)


async def _executions(scope: StepScope) -> list[ToolExecution]:
    async with unit_of_work() as s:
        return list(
            (
                await s.execute(
                    select(ToolExecution)
                    .where(ToolExecution.run_id == scope.run_id)
                    .order_by(ToolExecution.created_at)
                )
            ).scalars()
        )


async def _artifact_count(run_id: uuid.UUID) -> int:
    async with unit_of_work() as s:
        return int(
            (
                await s.execute(select(func.count()).select_from(Artifact).where(Artifact.run_id == run_id))
            ).scalar_one()
        )


def _result_text(content: dict[str, Any]) -> str:
    """The data a tool returned, without echoes of the request (a search echoes its own query string)."""
    return repr({k: v for k, v in content.items() if k not in ("query", "filename", "error")})


def _call(call_id: str, name: str, args: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(call_id=call_id, name=name, arguments=args)


WRITE = {"filename": "report.md", "content": "hello", "reason": "security test"}


async def p_unauthorized_tool(env: Env) -> CaseOutcome:
    scope = await env.scope({"document_search"})
    out = await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    rows = await _executions(scope)
    ok = (
        out.kind == "denied"
        and [r.status for r in rows] == ["denied"]
        and await _artifact_count(scope.run_id) == 0
    )
    return CaseOutcome(
        "pass" if ok else "fail",
        {"outcome": out.kind, "recorded": [r.status for r in rows]},
        {"tool_execution_error": rows[0].error if rows else None},
    )


async def p_unknown_tool(env: Env) -> CaseOutcome:
    scope = await env.scope({"document_search", "ghost_tool"})
    out = await execute_tool_call(_call("c1", "ghost_tool", {}), scope, get_registry())
    rows = await _executions(scope)
    return CaseOutcome(
        "pass" if out.kind == "denied" and rows and rows[0].status == "denied" else "fail",
        {"outcome": out.kind, "note": "listed as allowed but not registered"},
        {},
    )


async def p_schema_enforced(env: Env) -> CaseOutcome:
    scope = await env.scope({"calculator"})
    out = await execute_tool_call(
        _call("c1", "calculator", {"operation": "add", "values": "not-a-list"}), scope, get_registry()
    )
    rows = await _executions(scope)
    ok = (
        out.kind == "error"
        and rows
        and rows[0].status == "failed"
        and (rows[0].error or {}).get("type") == "invalid_tool_arguments"
    )
    return CaseOutcome(
        "pass" if ok else "fail", {"outcome": out.kind, "error": (rows[0].error if rows else None)}, {}
    )


async def _resolve(
    approval_id: uuid.UUID, decision: ApprovalStatus, user_id: uuid.UUID | None, reason: str
) -> None:
    async with unit_of_work() as s:
        await core.resolve_approval(s, approval_id, decision=decision, user_id=user_id, reason=reason)


async def p_approval_bound_to_arguments(env: Env) -> CaseOutcome:
    scope = await env.scope({"artifact_writer"})
    approval = await env.request_approval(scope, _call("c1", "artifact_writer", WRITE))
    await _resolve(approval.id, ApprovalStatus.APPROVED, env.user_id, "security test")
    scope = await env.reclaim(scope)  # the step resumes under a new lease, as a worker would
    tampered = {**WRITE, "content": "TAMPERED after approval"}
    b = await execute_tool_call(_call("c1", "artifact_writer", tampered), scope, get_registry())
    wrote_tampered = await _artifact_count(scope.run_id)
    c = await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    ok = (
        b.kind == "needs_approval"
        and wrote_tampered == 0
        and c.kind == "result"
        and await _artifact_count(scope.run_id) == 1
    )
    return CaseOutcome(
        "pass" if ok else "fail",
        {"tampered_call": b.kind, "artifacts_after_tampered": wrote_tampered, "approved_call": c.kind},
        {"approval_arguments_hash": approval.arguments_hash},
    )


async def p_rejected_action_not_executed(env: Env) -> CaseOutcome:
    scope = await env.scope({"artifact_writer"})
    approval = await env.request_approval(scope, _call("c1", "artifact_writer", WRITE))
    await _resolve(approval.id, ApprovalStatus.REJECTED, env.user_id, "no")
    b = await execute_tool_call(
        _call("c1", "artifact_writer", WRITE), scope, get_registry()
    )  # retrying the same call
    rows = await _executions(scope)
    ok = (
        b.kind == "error"
        and bool(b.extra.get("rejected"))
        and await _artifact_count(scope.run_id) == 0
        and all(r.status != "succeeded" for r in rows)
    )
    return CaseOutcome(
        "pass" if ok else "fail",
        {
            "after_rejection": b.kind,
            "artifacts": await _artifact_count(scope.run_id),
            "execution_statuses": [r.status for r in rows],
        },
        {},
    )


async def p_approval_single_use(env: Env) -> CaseOutcome:
    scope = await env.scope({"artifact_writer"})
    approval = await env.request_approval(scope, _call("c1", "artifact_writer", WRITE))
    await _resolve(approval.id, ApprovalStatus.APPROVED, env.user_id, "once")
    scope = await env.reclaim(scope)
    first = await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    replay = await execute_tool_call(
        _call("c2", "artifact_writer", WRITE), scope, get_registry()
    )  # a new call, same arguments
    async with unit_of_work() as s:
        consumed = (
            await s.execute(select(ApprovalRequest.consumed_at).where(ApprovalRequest.id == approval.id))
        ).scalar_one()
    ok = (
        first.kind == "result"
        and replay.content.get("idempotent_replay") is True
        and await _artifact_count(scope.run_id) == 1
        and consumed is not None
    )
    return CaseOutcome(
        "pass" if ok else "fail",
        {
            "first": first.kind,
            "second": replay.kind,
            "second_was_replay": replay.content.get("idempotent_replay"),
            "artifacts": await _artifact_count(scope.run_id),
        },
        {},
    )


async def p_expired_approval(env: Env) -> CaseOutcome:
    scope = await env.scope({"artifact_writer"})
    approval = await env.request_approval(scope, _call("c1", "artifact_writer", WRITE))
    async with unit_of_work() as s:
        row = await s.get(ApprovalRequest, approval.id)
        assert row is not None
        row.expires_at = utcnow() - timedelta(seconds=1)
    await core.expire_approvals()
    refused = False
    try:
        await _resolve(approval.id, ApprovalStatus.APPROVED, env.user_id, "too late")
    except Exception:
        refused = True
    after = await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    async with unit_of_work() as s:
        status = (
            await s.execute(select(ApprovalRequest.status).where(ApprovalRequest.id == approval.id))
        ).scalar_one()
    ok = (
        status == "expired"
        and refused
        and after.kind != "result"
        and await _artifact_count(scope.run_id) == 0
    )
    return CaseOutcome(
        "pass" if ok else "fail",
        {"approval_status": status, "late_approval_refused": refused, "execution_after": after.kind},
        {},
    )


async def p_tool_call_budget(env: Env) -> CaseOutcome:
    scope = await env.scope({"calculator"}, max_tool_calls=2)
    outs = [
        await execute_tool_call(
            _call(f"c{i}", "calculator", {"operation": "add", "values": [i, 1]}), scope, get_registry()
        )
        for i in range(4)
    ]
    kinds = [o.kind for o in outs]
    ok = (
        kinds[:2] == ["result", "result"]
        and kinds[2:] == ["denied", "denied"]
        and outs[2].extra.get("budget_exceeded")
    )
    return CaseOutcome("pass" if ok else "fail", {"outcomes": kinds, "limit": 2}, {})


async def p_workspace_isolation_search(env: Env) -> CaseOutcome:
    probe = await env.probe_workspace()
    scope = await env.scope({"document_search"})
    out = await execute_tool_call(
        _call("c1", "document_search", {"query": "Quarterly Blue Heron", "limit": 10}), scope, get_registry()
    )
    leaked = CANARY in _result_text(out.content)
    async with unit_of_work() as s:  # control: the canary really exists, in the other workspace
        exists = (
            await s.execute(select(func.count()).select_from(Document).where(Document.workspace_id == probe))
        ).scalar_one()
    return CaseOutcome(
        "pass" if out.kind == "result" and not leaked and exists >= 1 else "fail",
        {
            "outcome": out.kind,
            "results_returned": out.content.get("result_count"),
            "canary_leaked": leaked,
            "canary_documents_in_other_workspace": int(exists),
        },
        {},
    )


async def p_workspace_isolation_file(env: Env) -> CaseOutcome:
    await env.probe_workspace()
    scope = await env.scope({"structured_data_reader"})
    out = await execute_tool_call(
        _call(
            "c1", "structured_data_reader", {"filename": "other-workspace-secret.json", "source": "document"}
        ),
        scope,
        get_registry(),
    )
    leaked = CANARY in repr(out.content)
    return CaseOutcome(
        "pass" if out.kind == "error" and not leaked else "fail",
        {"outcome": out.kind, "message": str(out.content.get("error"))[:120], "canary_leaked": leaked},
        {},
    )


async def p_filename_traversal(env: Env) -> CaseOutcome:
    scope = await env.scope({"structured_data_reader"})
    outs = [
        await execute_tool_call(
            _call(f"c{i}", "structured_data_reader", {"filename": f, "source": "document"}),
            scope,
            get_registry(),
        )
        for i, f in enumerate(["../../../etc/passwd", "/etc/passwd", "..\\..\\windows\\win.ini"])
    ]
    ok = all(o.kind == "error" for o in outs)
    return CaseOutcome("pass" if ok else "fail", {"outcomes": [o.kind for o in outs]}, {})


POLICY_CASES: list[PolicyCase] = [
    PolicyCase(
        "policy/unauthorized_tool",
        "tool_authorization",
        "A tool outside the agent's allow-list is refused",
        "denied; nothing executed",
        p_unauthorized_tool,
    ),
    PolicyCase(
        "policy/unknown_tool",
        "tool_authorization",
        "A tool that is not registered is refused",
        "denied",
        p_unknown_tool,
    ),
    PolicyCase(
        "policy/schema_enforced",
        "tool_authorization",
        "Malformed arguments are rejected by schema validation",
        "failed: invalid_tool_arguments",
        p_schema_enforced,
    ),
    PolicyCase(
        "policy/approval_bound_to_arguments",
        "approval",
        "Arguments modified after approval are not executed",
        "the tampered call needs its own approval; the approved call executes once",
        p_approval_bound_to_arguments,
    ),
    PolicyCase(
        "policy/rejected_action_not_executed",
        "approval",
        "A rejected action cannot be executed by retrying",
        "never executed",
        p_rejected_action_not_executed,
    ),
    PolicyCase(
        "policy/approval_single_use",
        "approval",
        "An approval cannot be reused for a second execution",
        "second call replays the stored result; one artifact",
        p_approval_single_use,
    ),
    PolicyCase(
        "policy/expired_approval",
        "approval",
        "An expired approval cannot be approved or used",
        "not executed",
        p_expired_approval,
    ),
    PolicyCase(
        "policy/tool_call_budget",
        "budget",
        "The run's tool-call budget is enforced",
        "calls beyond the limit are denied",
        p_tool_call_budget,
    ),
    PolicyCase(
        "policy/workspace_isolation_search",
        "workspace_isolation",
        "Search cannot see another workspace's documents",
        "no results from the other workspace",
        p_workspace_isolation_search,
    ),
    PolicyCase(
        "policy/workspace_isolation_file",
        "workspace_isolation",
        "Data reader cannot open another workspace's files",
        "not found",
        p_workspace_isolation_file,
    ),
    PolicyCase(
        "policy/filename_traversal",
        "workspace_isolation",
        "Path traversal in file names is rejected",
        "rejected",
        p_filename_traversal,
    ),
]
