"""Reconstruct a run's flight recording from persisted, immutable records (read-only).

A recording is a list of *frames* (the run's events in sequence order, each with the step-status changes it
caused), plus the structured records that explain them: step attempts, sanitized model calls, tool executions
and approvals. Playback applies the per-frame deltas; nothing here executes anything.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import (
    ApprovalRequest,
    Event,
    ModelCallRecord,
    StepAttempt,
    ToolExecution,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.redaction import redact

SCHEMA_VERSION = 1
STATUS_BY_EVENT = {
    "step.ready": "ready",
    "step.started": "running",
    "step.completed": "succeeded",
    "step.failed": "failed",
    "step.retrying": "retry_wait",
    "step.skipped": "skipped",
    "step.cancelled": "cancelled",
    "step.suspended": "waiting_approval",
    "step.waiting_child": "waiting_child",
}
ERROR_EVENTS = {
    "step.failed",
    "model.call.failed",
    "tool.failed",
    "tool.denied",
    "budget.exceeded",
    "run.failed",
}
WARNING_EVENTS = {"step.retrying", "step.stale_result_rejected", "run.cancelled"}
PREVIEW = 600


def iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _preview(value: Any, limit: int = PREVIEW) -> Any:
    text = json.dumps(value, default=str)
    return value if len(text) <= limit else {"preview": text[:limit] + "…", "truncated": True}


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _ms(base: datetime | None, at: datetime | None) -> int | None:
    return int((at - base).total_seconds() * 1000) if base and at else None


async def load_recording(session: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    run = await session.get(WorkflowRun, run_id)
    if run is None:
        raise LookupError("run not found")
    wf_name = (
        await session.execute(
            select(WorkflowDefinition.name).where(WorkflowDefinition.id == run.workflow_definition_id)
        )
    ).scalar_one_or_none()
    steps = list((await session.execute(select(WorkflowStep).where(WorkflowStep.run_id == run_id))).scalars())
    key_of = {s.id: s.node_key for s in steps}
    events = list(
        (
            await session.execute(select(Event).where(Event.run_id == run_id).order_by(Event.sequence_number))
        ).scalars()
    )
    t0 = events[0].timestamp if events else run.created_at

    status: dict[str, str] = {s.node_key: "pending" for s in steps if "fan_out_parent" not in s.node_config}
    initial = dict(status)
    frames: list[dict[str, Any]] = []
    for e in events:
        key = key_of.get(e.step_id) if e.step_id else (e.payload or {}).get("node_key")
        delta: dict[str, str] = {}
        new = STATUS_BY_EVENT.get(e.event_type)
        if new and key:
            delta[key] = new
        if e.event_type == "step.fan_out" and key:
            delta[key] = "waiting_child"
            delta.update({c: "pending" for c in (e.payload or {}).get("children", [])})
        status.update(delta)
        severity = (
            "error"
            if e.event_type in ERROR_EVENTS
            else "warning"
            if e.event_type in WARNING_EVENTS
            else "info"
        )
        if e.event_type == "tool.completed" and (e.payload or {}).get("denied"):
            severity = "error"
        frames.append(
            {
                "seq": e.sequence_number,
                "ts": iso(e.timestamp),
                "elapsed_ms": _ms(t0, e.timestamp),
                "type": e.event_type,
                "step_key": key,
                "severity": severity,
                "status_delta": delta,
                "trace_id": e.trace_id,
                "span_id": e.span_id,
                "payload": _preview(redact(e.payload or {}), 2000),
            }
        )

    seqs = [f["seq"] for f in frames]
    gaps = [(a, b) for a, b in itertools.pairwise(seqs) if b != a + 1]
    attempts = list(
        (
            await session.execute(
                select(StepAttempt).where(StepAttempt.run_id == run_id).order_by(StepAttempt.started_at)
            )
        ).scalars()
    )
    records = list(
        (
            await session.execute(
                select(ModelCallRecord)
                .where(ModelCallRecord.run_id == run_id)
                .order_by(ModelCallRecord.created_at, ModelCallRecord.call_index)
            )
        ).scalars()
    )
    tools = list(
        (
            await session.execute(
                select(ToolExecution).where(ToolExecution.run_id == run_id).order_by(ToolExecution.created_at)
            )
        ).scalars()
    )
    approvals = list(
        (
            await session.execute(
                select(ApprovalRequest)
                .where(ApprovalRequest.run_id == run_id)
                .order_by(ApprovalRequest.requested_at)
            )
        ).scalars()
    )

    recording: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "mode": "recorded",
        "run": {
            "id": str(run.id),
            "workflow": wf_name,
            "workflow_version": run.workflow_version,
            "status": run.status,
            "purpose": run.purpose,
            "replay_of_run_id": str(run.replay_of_run_id) if run.replay_of_run_id else None,
            "parent_run_id": str(run.parent_run_id) if run.parent_run_id else None,
            "priority": run.priority,
            "trace_id": run.trace_id,
            "created_at": iso(run.created_at),
            "completed_at": iso(run.completed_at),
            "duration_ms": _ms(run.created_at, run.completed_at),
            "cost_spent_usd": str(run.cost_spent_usd),
            "cost_basis_note": "Estimated cost; simulated pricing for mock/fixture/recorded providers.",
            "model_calls": run.model_calls,
            "tool_calls": run.tool_calls,
            "input": _preview(redact(run.input), 2000),
            "output": _preview(redact(run.output), 4000) if run.output is not None else None,
            "error": redact(run.error) if run.error else None,
        },
        "steps": [
            {
                "key": s.node_key,
                "type": s.node_type,
                "status": s.status,
                "agent_version_id": str(s.agent_version_id) if s.agent_version_id else None,
                "depends_on": list(s.dependency_keys),
                "attempts": s.attempt_count,
                "failures": s.failure_count,
                "fan_out_parent": s.node_config.get("fan_out_parent"),
                "when": s.node_config.get("when"),
                "started_ms": _ms(t0, s.started_at),
                "completed_ms": _ms(t0, s.completed_at),
                "duration_ms": _ms(s.started_at, s.completed_at),
                "output": _preview(redact(s.output)) if s.output is not None else None,
                "error": redact(s.error) if s.error else None,
            }
            for s in sorted(steps, key=lambda x: (x.started_at is None, x.started_at or x.created_at))
        ],
        "attempts": [
            {
                "step_key": key_of.get(a.step_id),
                "attempt": a.attempt_number,
                "worker_id": a.worker_id,
                "status": a.status,
                "started_ms": _ms(t0, a.started_at),
                "finished_ms": _ms(t0, a.finished_at),
                "queue_wait_ms": a.queue_wait_ms,
                "error_type": a.error_type,
            }
            for a in attempts
        ],
        "model_calls": [
            {
                "id": str(r.id),
                "step_key": r.node_key,
                "call_index": r.call_index,
                "attempt": r.attempt_number,
                "provider": r.provider,
                "model": r.model,
                "agent_version_id": str(r.agent_version_id) if r.agent_version_id else None,
                "status": r.status,
                "latency_ms": r.latency_ms,
                "request_digest": r.request_digest,
                "truncated": r.truncated,
                "at_ms": _ms(t0, r.created_at),
                "finish_reason": (r.response or {}).get("finish_reason"),
                "input_tokens": ((r.response or {}).get("usage") or {}).get("input_tokens"),
                "output_tokens": ((r.response or {}).get("usage") or {}).get("output_tokens"),
                "tool_calls": [c["name"] for c in (r.response or {}).get("tool_calls", [])],
                "error_type": (r.error or {}).get("type"),
            }
            for r in records
        ],
        "tool_executions": [
            {
                "id": str(t.id),
                "step_key": key_of.get(t.step_id),
                "tool": t.tool_name,
                "status": t.status,
                "arguments_hash": t.arguments_hash,
                "arguments": _preview(redact(t.arguments)),
                "result": _preview(redact(t.result)) if t.result is not None else None,
                "error": redact(t.error) if t.error else None,
                "requires_approval": t.requires_approval,
                "reason": t.reason,
                "duration_ms": _ms(t.started_at, t.completed_at),
                "at_ms": _ms(t0, t.created_at),
            }
            for t in tools
        ],
        "approvals": [
            {
                "id": str(a.id),
                "step_key": key_of.get(a.step_id),
                "action": a.requested_action,
                "status": a.status,
                "requested_ms": _ms(t0, a.requested_at),
                "decided_ms": _ms(t0, a.decided_at),
                "arguments_hash": a.arguments_hash,
                "decision_reason": redact(a.decision_reason) if a.decision_reason else None,
            }
            for a in approvals
        ],
        "initial_status": initial,
        "frames": frames,
        "integrity": {
            "event_count": len(frames),
            "first_seq": seqs[0] if seqs else None,
            "last_seq": seqs[-1] if seqs else None,
            "contiguous": not gaps and (not seqs or seqs[0] == 1),
            "gaps": gaps,
        },
    }
    recording["integrity"]["sha256"] = canonical_hash(frames)
    return recording


def state_at(recording: dict[str, Any], seq: int) -> dict[str, str]:
    """Step statuses after applying every frame up to and including ``seq`` (what the playback UI shows)."""
    status = dict(recording["initial_status"])
    for f in recording["frames"]:
        if f["seq"] > seq:
            break
        status.update(f["status_delta"])
    return status
