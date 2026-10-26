"""Security incidents derived from real platform records (never fabricated).

A policy denial, a rejected or expired approval, a budget stop and a failed security test are each recorded by the
engine when they happen; this module only reads them, with the run, step, agent and evidence that identify each one.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import (
    ApprovalRequest,
    Event,
    SecurityEvaluationResult,
    SecurityEvaluationRun,
    ToolExecution,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.util import utcnow

SOURCES = (
    "policy_denial",
    "approval_rejected",
    "approval_expired",
    "budget_exceeded",
    "security_test_failure",
)


def _origin(purpose: str) -> str:
    return {
        "security": "security suite",
        "chaos": "chaos experiment",
        "replay": "re-execution",
        "evaluation": "evaluation",
    }.get(purpose, "workflow run")


async def list_incidents(
    session: AsyncSession, workspace_id: uuid.UUID, *, days: int = 30, limit: int = 200
) -> list[dict[str, Any]]:
    since = utcnow() - timedelta(days=days)
    out: list[dict[str, Any]] = []

    rows = (
        await session.execute(
            select(Event, WorkflowRun.purpose, WorkflowStep.node_key)
            .join(WorkflowRun, WorkflowRun.id == Event.run_id)
            .outerjoin(WorkflowStep, WorkflowStep.id == Event.step_id)
            .where(
                WorkflowRun.workspace_id == workspace_id,
                Event.timestamp >= since,
                Event.event_type.in_(["tool.denied", "budget.exceeded"]),
            )
            .order_by(Event.timestamp.desc())
            .limit(limit)
        )
    ).all()
    te_ids = [
        uuid.UUID(e.payload["tool_execution_id"])
        for e, _, _ in rows
        if e.event_type == "tool.denied" and e.payload.get("tool_execution_id")
    ]
    tes = (
        {
            t.id: t
            for t in (
                await session.execute(select(ToolExecution).where(ToolExecution.id.in_(te_ids)))
            ).scalars()
        }
        if te_ids
        else {}
    )
    for e, purpose, step_key in rows:
        if e.event_type == "tool.denied":
            te = (
                tes.get(uuid.UUID(e.payload["tool_execution_id"]))
                if e.payload.get("tool_execution_id")
                else None
            )
            reason = (
                (e.payload.get("error") or {}).get("message") or (te.error or {}).get("message")
                if te
                else None
            )
            out.append(
                {
                    "id": f"event:{e.id}",
                    "at": e.timestamp.isoformat(),
                    "source": "policy_denial",
                    "severity": "warning",
                    "title": f"Tool call to {e.payload.get('tool', '?')} was denied by policy",
                    "detail": reason,
                    "run_id": str(e.run_id),
                    "step_key": step_key,
                    "origin": _origin(purpose),
                    "evidence": {
                        "event_seq": e.sequence_number,
                        "tool_execution_id": e.payload.get("tool_execution_id"),
                    },
                }
            )
        else:
            out.append(
                {
                    "id": f"event:{e.id}",
                    "at": e.timestamp.isoformat(),
                    "source": "budget_exceeded",
                    "severity": "info",
                    "title": "A run was stopped by its budget",
                    "detail": str(e.payload.get("limit")),
                    "run_id": str(e.run_id),
                    "step_key": step_key,
                    "origin": _origin(purpose),
                    "evidence": {"event_seq": e.sequence_number},
                }
            )

    approvals = (
        await session.execute(
            select(ApprovalRequest, WorkflowRun.purpose)
            .join(WorkflowRun, WorkflowRun.id == ApprovalRequest.run_id)
            .where(
                ApprovalRequest.workspace_id == workspace_id,
                ApprovalRequest.status.in_(["rejected", "expired"]),
                ApprovalRequest.requested_at >= since,
            )
            .order_by(ApprovalRequest.requested_at.desc())
            .limit(limit)
        )
    ).all()
    for a, purpose in approvals:
        out.append(
            {
                "id": f"approval:{a.id}",
                "at": (a.decided_at or a.requested_at).isoformat(),
                "source": f"approval_{a.status}",
                "severity": "info" if a.status == "expired" else "warning",
                "title": f"Approval for '{a.requested_action[:80]}' was {a.status}",
                "detail": a.decision_reason,
                "run_id": str(a.run_id),
                "step_key": None,
                "origin": _origin(purpose),
                "evidence": {"approval_id": str(a.id), "arguments_hash": a.arguments_hash},
            }
        )

    latest = (
        await session.execute(
            select(SecurityEvaluationRun)
            .where(
                SecurityEvaluationRun.workspace_id == workspace_id,
                SecurityEvaluationRun.status == "completed",
            )
            .order_by(SecurityEvaluationRun.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if latest is not None:
        fails = (
            (
                await session.execute(
                    select(SecurityEvaluationResult).where(
                        SecurityEvaluationResult.evaluation_run_id == latest.id,
                        SecurityEvaluationResult.verdict != "pass",
                    )
                )
            )
            .scalars()
            .all()
        )
        for r in fails:
            out.append(
                {
                    "id": f"security:{r.id}",
                    "at": (latest.completed_at or latest.created_at).isoformat(),
                    "source": "security_test_failure",
                    "severity": "high" if r.verdict == "fail" else "warning",
                    "title": f"Security test {r.verdict}: {r.case_key}",
                    "detail": str(r.observed)[:300],
                    "run_id": str(r.workflow_run_id) if r.workflow_run_id else None,
                    "step_key": None,
                    "origin": "security suite",
                    "evidence": {"evaluation_run_id": str(latest.id), "case_key": r.case_key},
                }
            )
    out.sort(key=lambda i: i["at"], reverse=True)
    return out[:limit]
