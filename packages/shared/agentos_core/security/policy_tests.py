"""Engine-level enforcement tests: each case drives the real tool-execution path and checks what the code did.

Nothing is mocked: the cases call ``execute_tool_call`` / the approval functions against PostgreSQL and then read the
persisted records (tool executions, approvals, artifacts) as evidence. A case *passes* when the platform refused the
action, and it must also show that the action was actually attempted (otherwise it would pass vacuously).
"""

from __future__ import annotations

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
    """A paused, synthetic run (so no worker claims its step) whose attempt gives tests a real tool scope."""

    def __init__(self, workspace_id: uuid.UUID, user_id: uuid.UUID | None, storage: Storage) -> None:
        self.workspace_id, self.user_id, self.storage = workspace_id, user_id, storage
        self.probe_workspace_id: uuid.UUID | None = None

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
            await core.pause_run(s, run.id, user_id=None, reason="security scope: never executed")
            step = (await s.execute(select(WorkflowStep).where(WorkflowStep.run_id == run.id))).scalar_one()
            attempt = StepAttempt(
                step_id=step.id,
                run_id=run.id,
                attempt_number=1,
                worker_id="security-suite",
                lease_token=uuid.uuid4(),
                status="running",
                started_at=utcnow(),
            )
            s.add(attempt)
            await s.flush()
            return StepScope(
                workspace_id=self.workspace_id,
                run_id=run.id,
                step_id=step.id,
                node_key="probe",
                attempt_id=attempt.id,
                lease_token=attempt.lease_token,
                agent_name="Security Probe",
                allowed_tools=frozenset(allowed),
                storage=self.storage,
            )

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
                data=f"# Canary\n\nThe secret marker is {CANARY}.".encode(),
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


def _call(call_id: str, name: str, args: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(call_id=call_id, name=name, arguments=args)


WRITE = {"filename": "report.md", "content": "hello", "content_type": "text/markdown"}


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


async def p_approval_bound_to_arguments(env: Env) -> CaseOutcome:
    scope = await env.scope({"artifact_writer"})
    a = await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    if a.kind != "needs_approval":
        return CaseOutcome("error", {"first": a.kind}, {})
    async with unit_of_work() as s:
        approval = (
            await s.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == scope.run_id))
        ).scalar_one()
        await core.resolve_approval(
            s, approval.id, decision=ApprovalStatus.APPROVED, user_id=env.user_id, reason="security test"
        )
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
    a = await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    async with unit_of_work() as s:
        approval = (
            await s.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == scope.run_id))
        ).scalar_one()
        await core.resolve_approval(
            s, approval.id, decision=ApprovalStatus.REJECTED, user_id=env.user_id, reason="no"
        )
    b = await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    ok = (
        a.kind == "needs_approval"
        and b.kind == "error"
        and b.extra.get("rejected")
        and await _artifact_count(scope.run_id) == 0
    )
    return CaseOutcome(
        "pass" if ok else "fail",
        {"after_rejection": b.kind, "artifacts": await _artifact_count(scope.run_id)},
        {},
    )


async def p_approval_single_use(env: Env) -> CaseOutcome:
    scope = await env.scope({"artifact_writer"})
    await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    async with unit_of_work() as s:
        approval = (
            await s.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == scope.run_id))
        ).scalar_one()
        await core.resolve_approval(
            s, approval.id, decision=ApprovalStatus.APPROVED, user_id=env.user_id, reason="once"
        )
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
    await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    async with unit_of_work() as s:
        approval = (
            await s.execute(select(ApprovalRequest).where(ApprovalRequest.run_id == scope.run_id))
        ).scalar_one()
        approval.expires_at = utcnow() - timedelta(seconds=1)
    async with unit_of_work() as s:
        await core.expire_approvals(now=utcnow())
    refused = False
    try:
        async with unit_of_work() as s:
            await core.resolve_approval(
                s, approval.id, decision=ApprovalStatus.APPROVED, user_id=env.user_id, reason="too late"
            )
    except Exception:
        refused = True
    after = await execute_tool_call(_call("c1", "artifact_writer", WRITE), scope, get_registry())
    async with unit_of_work() as s:
        status = (
            await s.execute(select(ApprovalRequest.status).where(ApprovalRequest.id == approval.id))
        ).scalar_one()
    ok = status != "approved" and after.kind != "result" and await _artifact_count(scope.run_id) == 0
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
        _call("c1", "document_search", {"query": CANARY, "limit": 10}), scope, get_registry()
    )
    leaked = CANARY in repr(out.content)
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
