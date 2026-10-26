"""PostgreSQL schema. PostgreSQL is the single source of truth for all workflow state.

Audit-history tables (attempts, events, tool executions, approvals, model usage)
reference runs with ``ON DELETE CASCADE`` only from the run downwards; nothing
deletes runs in normal operation, and agent/workflow definitions are protected
with ``RESTRICT`` so history can never lose its referents.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from agentos_core.db.base import Base, created_at_col, updated_at_col

EMPTY_OBJECT = text("'{}'::jsonb")
EMPTY_LIST = text("'[]'::jsonb")


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="developer")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    autopilot_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class WorkspaceMember(Base):
    __tablename__ = "workspace_members"
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    __table_args__ = (
        CheckConstraint("role in ('admin','developer','viewer')", name="role_valid"),
        Index("ix_workspace_members_user", "user_id"),
    )


class AgentDefinition(Base):
    __tablename__ = "agent_definitions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    system_instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    model_configuration: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=EMPTY_OBJECT, nullable=False
    )
    allowed_tool_ids: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_LIST, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal("1"))
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    # Versioning (migration 0002). The columns above mirror the active version for API compatibility.
    active_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT", use_alter=True)
    )
    canary_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT", use_alter=True)
    )
    canary_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", name="uq_agent_definitions_workspace_name"),
        CheckConstraint("canary_percent between 0 and 100", name="canary_percent_range"),
        CheckConstraint("provider in ('anthropic','openai','mock','fixture')", name="provider_valid"),
        CheckConstraint("max_steps between 1 and 50", name="max_steps_range"),
        CheckConstraint("timeout_seconds between 1 and 3600", name="timeout_range"),
        CheckConstraint("max_cost_usd >= 0", name="max_cost_nonneg"),
    )


class WorkflowDefinition(Base):
    """Immutable by version: an update inserts a new row with version+1 and the same name."""

    __tablename__ = "workflow_definitions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", "version", name="uq_workflow_definitions_name_version"),
        Index("ix_workflow_definitions_ws_created", "workspace_id", "created_at"),
    )


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    workflow_definition_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    workflow_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    # Budget policy (snapshot at creation) and live counters.
    budget: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_reserved_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), nullable=False, default=Decimal(0), server_default="0"
    )
    cost_spent_usd: Mapped[Decimal] = mapped_column(
        Numeric(14, 8), nullable=False, default=Decimal(0), server_default="0"
    )
    event_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trace_id: Mapped[str | None] = mapped_column(String(32))
    # 'workflow' for normal runs, 'evaluation' for OpenEval case executions.
    purpose: Mapped[str] = mapped_column(
        String(16), nullable=False, default="workflow", server_default="workflow"
    )
    # Scheduling: 0 (lowest) .. 9. Waiting steps gain effective priority over time (starvation protection).
    priority: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    # Paused runs keep their state; no new step is claimed until resumed. Running steps finish.
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(String(500))
    # Nested workflows: the child run points at the parent run and the sub_workflow step that started it.
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE")
    )
    parent_step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "workflow_steps.id", ondelete="CASCADE", use_alter=True, name="fk_workflow_runs_parent_step"
        )
    )
    depth: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=0, server_default="0")
    # Deterministic re-execution (Flight Recorder): the recorded run this run replays.
    replay_of_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = created_at_col()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = updated_at_col()
    __table_args__ = (
        CheckConstraint(
            "status in ('pending','planning','running','waiting_approval','succeeded','failed','cancelled')",
            name="status_valid",
        ),
        CheckConstraint("purpose in ('workflow','evaluation','replay')", name="purpose_valid"),
        CheckConstraint("priority between 0 and 9", name="priority_range"),
        CheckConstraint("depth between 0 and 5", name="depth_range"),
        Index("ix_workflow_runs_parent", "parent_run_id", postgresql_where=text("parent_run_id is not null")),
        Index("ix_workflow_runs_ws_created", "workspace_id", "created_at"),
        Index("ix_workflow_runs_status", "status"),
        Index("ix_workflow_runs_definition", "workflow_definition_id"),
    )


class WorkflowStep(Base):
    __tablename__ = "workflow_steps"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    node_type: Mapped[str] = mapped_column(String(32), nullable=False)
    node_config: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    agent_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT")
    )
    # Exact agent configuration this step runs with (pinned at run creation; NULL for pre-0002 runs).
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT")
    )
    dependency_keys: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_LIST, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    # Durable conversation checkpoint so an agent can resume after approval or crash.
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retry_deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[uuid.UUID | None] = mapped_column()
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()
    __table_args__ = (
        UniqueConstraint("run_id", "node_key", name="uq_workflow_steps_run_node"),
        CheckConstraint(
            "status in ('pending','ready','running','waiting_approval','retry_wait','waiting_child',"
            "'succeeded','failed','skipped','cancelled')",
            name="status_valid",
        ),
        CheckConstraint("max_attempts between 1 and 20", name="max_attempts_range"),
        CheckConstraint("(status = 'running') = (lease_token is not null)", name="lease_iff_running"),
        # Scheduling queries: claimable steps and expired leases.
        Index(
            "ix_workflow_steps_claimable",
            "status",
            "next_run_at",
            postgresql_where=text("status in ('ready','retry_wait')"),
        ),
        Index("ix_workflow_steps_lease", "lease_expires_at", postgresql_where=text("status = 'running'")),
        Index("ix_workflow_steps_run_status", "run_id", "status"),
        Index("ix_workflow_steps_agent", "agent_definition_id"),
    )


class StepAttempt(Base):
    __tablename__ = "step_attempts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_steps.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(200), nullable=False)
    lease_token: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    queue_wait_ms: Mapped[int | None] = mapped_column(Integer)
    error_type: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    execution_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=EMPTY_OBJECT, nullable=False
    )
    __table_args__ = (
        UniqueConstraint("step_id", "attempt_number", name="uq_step_attempts_step_number"),
        UniqueConstraint("lease_token", name="uq_step_attempts_lease_token"),
        Index("ix_step_attempts_run", "run_id"),
        Index("ix_step_attempts_started", "started_at"),
    )


class ToolExecution(Base):
    __tablename__ = "tool_executions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_steps.id", ondelete="CASCADE"), nullable=False
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("step_attempts.id", ondelete="SET NULL"))
    tool_call_id: Mapped[str | None] = mapped_column(String(200))
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (
        Index("ix_tool_executions_run", "run_id"),
        Index("ix_tool_executions_step", "step_id"),
        Index("ix_tool_executions_idem", "idempotency_key"),
        # At most one *successful* execution per idempotency key.
        Index(
            "uq_tool_executions_idem_succeeded",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
    )


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_steps.id", ondelete="CASCADE"), nullable=False
    )
    tool_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_executions.id", ondelete="CASCADE")
    )
    requested_action: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    affected_resource: Mapped[str | None] = mapped_column(String(500))
    requesting_agent: Mapped[str | None] = mapped_column(String(200))
    sanitized_arguments: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=EMPTY_OBJECT, nullable=False
    )
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status in ('pending','approved','rejected','expired')", name="status_valid"),
        UniqueConstraint("tool_execution_id", name="uq_approval_requests_tool_execution"),
        # One open approval per step at a time; duplicate requests collapse onto it.
        Index(
            "uq_approval_requests_step_pending",
            "step_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_approval_requests_ws_status", "workspace_id", "status"),
        Index("ix_approval_requests_run", "run_id"),
        Index("ix_approval_requests_expires", "expires_at", postgresql_where=text("status = 'pending'")),
    )


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_steps.id", ondelete="CASCADE"))
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(32))
    span_id: Mapped[str | None] = mapped_column(String(16))
    __table_args__ = (
        UniqueConstraint("run_id", "sequence_number", name="uq_events_run_seq"),
        Index("ix_events_type_ts", "event_type", "timestamp"),
    )


class ModelUsage(Base):
    __tablename__ = "model_usage"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # NULL for Autopilot calls (diagnosis, optimizer) that are not part of a workflow run.
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, default="run", server_default="run")
    # Optimization cycle this Autopilot call belongs to (diagnosis id), for per-cycle spend limits.
    autopilot_ref: Mapped[uuid.UUID | None] = mapped_column()
    step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_steps.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("step_attempts.id", ondelete="SET NULL"))
    agent_definition_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    # NULL means "provider did not report" - never coerced to zero.
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_input_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_write_tokens: Mapped[int | None] = mapped_column(Integer)
    cached_output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    cost_basis: Mapped[str] = mapped_column(String(32), nullable=False)  # actual|partial|simulated|unknown
    pricing_version: Mapped[str] = mapped_column(String(64), nullable=False)
    finish_reason: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="succeeded")
    error_type: Mapped[str | None] = mapped_column(String(100))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("ix_model_usage_run", "run_id"),
        Index("ix_model_usage_recorded", "recorded_at"),
        Index("ix_model_usage_agent", "agent_definition_id"),
        Index("ix_model_usage_ws_purpose", "workspace_id", "purpose"),
    )


class ModelCallRecord(Base):
    """Immutable, sanitized record of one model request/response (the Flight Recorder's source of truth).

    Secrets are redacted before storage, raw provider payloads (thinking blocks, signatures) are never stored,
    and oversize requests are truncated with ``truncated`` set. Rows cannot be updated or deleted.
    """

    __tablename__ = "model_call_records"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_steps.id", ondelete="CASCADE"))
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("step_attempts.id", ondelete="CASCADE"))
    node_key: Mapped[str] = mapped_column(String(100), nullable=False)
    call_index: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_number: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT")
    )
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (
        CheckConstraint("status in ('succeeded','failed')", name="status_valid"),
        Index("ix_model_call_records_run", "run_id", "created_at"),
        Index("ix_model_call_records_step", "step_id"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_steps.id", ondelete="SET NULL"))
    tool_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tool_executions.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (
        UniqueConstraint("run_id", "idempotency_key", name="uq_artifacts_run_idem"),
        UniqueConstraint("run_id", "filename", name="uq_artifacts_run_filename"),
        Index("ix_artifacts_ws_created", "workspace_id", "created_at"),
    )


class Document(Base):
    """Untrusted user-provided source material, searchable by the document_search tool."""

    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (UniqueConstraint("workspace_id", "filename", name="uq_documents_ws_filename"),)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    section: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', coalesce(section,'') || ' ' || content)", persisted=True)
    )
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_doc_index"),
        Index("ix_document_chunks_search", "search_vector", postgresql_using="gin"),
        Index("ix_document_chunks_ws", "workspace_id"),
    )


class Worker(Base):
    __tablename__ = "workers"
    id: Mapped[str] = mapped_column(String(200), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(Integer, nullable=False)
    concurrency: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    in_flight: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    steps_completed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (Index("ix_workers_heartbeat", "last_heartbeat_at"),)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scope: Mapped[str] = mapped_column(String(100), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response_status: Mapped[int] = mapped_column(Integer, nullable=False)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "scope", "key", name="uq_idempotency_scope_key"),
    )


# =============================================================================== Autopilot
# Additive schema for versioned agent configs, OpenEval, failure intelligence,
# optimization, experiments and deployments (migration 0002). Rows that form the
# audit trail are protected by UPDATE/DELETE triggers in the migration.


class AgentConfigVersion(Base):
    """Immutable, content-addressed snapshot of everything that determines agent behavior."""

    __tablename__ = "agent_config_versions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT")
    )
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    config_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False
    )  # initial|manual|optimizer|manual_candidate
    change_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (
        UniqueConstraint("agent_id", "version", name="uq_agent_config_versions_agent_version"),
        UniqueConstraint("agent_id", "config_hash", name="uq_agent_config_versions_agent_hash"),
        CheckConstraint("source in ('initial','manual','optimizer','manual_candidate')", name="source_valid"),
        Index("ix_agent_config_versions_agent", "agent_id", "version"),
    )


class EvaluationDataset(Base):
    """Immutable, versioned set of benchmark cases for one split of a suite."""

    __tablename__ = "evaluation_datasets"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)  # suite name
    split: Mapped[str] = mapped_column(String(16), nullable=False)  # development|validation|test
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)  # visible|hidden
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dataset_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (
        UniqueConstraint("workspace_id", "name", "split", "version", name="uq_evaluation_datasets_identity"),
        CheckConstraint("split in ('development','validation','test')", name="split_valid"),
        CheckConstraint("visibility in ('visible','hidden')", name="visibility_valid"),
    )


class EvaluationCase(Base):
    __tablename__ = "evaluation_cases"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    case_key: Mapped[str] = mapped_column(String(120), nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    expected_output: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evaluator_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    tags: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_LIST, nullable=False)
    __table_args__ = (UniqueConstraint("dataset_id", "case_key", name="uq_evaluation_cases_dataset_key"),)


class OptimizationExperiment(Base):
    __tablename__ = "optimization_experiments"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("optimization_candidates.id", ondelete="RESTRICT", use_alter=True)
    )
    baseline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    validation_dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    trials: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="running"
    )  # running|completed|failed
    decision: Mapped[str | None] = mapped_column(String(32))  # eligible|not_eligible|inconclusive
    results: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    eligibility: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("status in ('running','completed','failed')", name="status_valid"),
        CheckConstraint("trials between 1 and 10", name="trials_range"),
        Index("ix_optimization_experiments_ws_created", "workspace_id", "created_at"),
        Index("ix_optimization_experiments_agent", "agent_id"),
    )


class EvaluationRun(Base):
    __tablename__ = "evaluation_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    agent_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_datasets.id", ondelete="RESTRICT"), nullable=False
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("optimization_experiments.id", ondelete="RESTRICT")
    )
    arm: Mapped[str | None] = mapped_column(String(16))  # baseline|candidate (experiments only)
    trials: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    max_cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status in ('queued','running','completed','failed','cancelled')", name="status_valid"
        ),
        CheckConstraint("trials between 1 and 10", name="trials_range"),
        CheckConstraint("arm is null or arm in ('baseline','candidate')", name="arm_valid"),
        Index("ix_evaluation_runs_status", "status"),
        Index("ix_evaluation_runs_ws_created", "workspace_id", "created_at"),
        Index("ix_evaluation_runs_experiment", "experiment_id"),
    )


class EvaluationCaseResult(Base):
    __tablename__ = "evaluation_case_results"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    evaluation_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_cases.id", ondelete="RESTRICT"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_key: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    workflow_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")  # queued|running|graded
    passed: Mapped[bool | None] = mapped_column(Boolean)
    observed_output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    scores: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "evaluation_run_id", "case_id", "attempt", name="uq_evaluation_case_results_identity"
        ),
        CheckConstraint("status in ('queued','running','graded')", name="status_valid"),
        Index("ix_evaluation_case_results_run_status", "evaluation_run_id", "status"),
        Index("ix_evaluation_case_results_workflow_run", "workflow_run_id"),
    )


class FailureSignal(Base):
    """One deterministic detection (error or anomaly) with links to its evidence."""

    __tablename__ = "failure_signals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="SET NULL")
    )
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_steps.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(48), nullable=False)
    signal_type: Mapped[str] = mapped_column(String(16), nullable=False)  # error|anomaly|evaluation
    detector: Mapped[str] = mapped_column(String(64), nullable=False)
    is_infrastructure: Mapped[bool] = mapped_column(Boolean, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    measurements: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 8))
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("workspace_id", "fingerprint", name="uq_failure_signals_fingerprint"),
        Index("ix_failure_signals_ws_category", "workspace_id", "category"),
        Index("ix_failure_signals_agent", "agent_id", "occurred_at"),
    )


class FailureDiagnosis(Base):
    """An evidence-linked hypothesis about a cluster of failures. Never a confirmed root cause."""

    __tablename__ = "failure_diagnoses"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    agent_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workflow_runs.id", ondelete="SET NULL"))
    failure_category: Mapped[str] = mapped_column(String(48), nullable=False)
    signal_ids: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    proposed_remedy: Mapped[str] = mapped_column(Text, nullable=False)
    limitations: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # hypothesis|invalid
    validation_errors: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_LIST, nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (
        CheckConstraint("confidence in ('low','medium','high')", name="confidence_valid"),
        CheckConstraint("status in ('hypothesis','invalid')", name="status_valid"),
        Index("ix_failure_diagnoses_ws_created", "workspace_id", "created_at"),
    )


class OptimizationCandidate(Base):
    __tablename__ = "optimization_candidates"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    diagnosis_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("failure_diagnoses.id", ondelete="RESTRICT")
    )
    baseline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT")
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    changes: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    diff: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    expected_benefits: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    risk_assessment: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    policy_violations: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_LIST, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    generator: Mapped[str] = mapped_column(String(16), nullable=False)  # optimizer|manual
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(200))
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (
        CheckConstraint(
            "status in ('proposed','rejected_policy','evaluating','eligible','not_eligible','inconclusive',"
            "'promoted','superseded')",
            name="status_valid",
        ),
        Index("ix_optimization_candidates_ws_created", "workspace_id", "created_at"),
        Index("ix_optimization_candidates_agent", "agent_id"),
    )


class DeploymentRequest(Base):
    """A pending promotion that an administrator must approve (single use, expires)."""

    __tablename__ = "deployment_requests"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("optimization_experiments.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    baseline_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    mode: Mapped[str] = mapped_column(String(16), nullable=False)  # full|canary
    canary_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        CheckConstraint("status in ('pending','approved','rejected','expired')", name="status_valid"),
        CheckConstraint("mode in ('full','canary')", name="mode_valid"),
        CheckConstraint("canary_percent between 0 and 100", name="canary_percent_range"),
        Index(
            "uq_deployment_requests_agent_pending",
            "agent_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
        ),
    )


class AgentDeployment(Base):
    __tablename__ = "agent_deployments"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_definitions.id", ondelete="RESTRICT"), nullable=False
    )
    config_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT"), nullable=False
    )
    previous_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent_config_versions.id", ondelete="RESTRICT")
    )
    environment: Mapped[str] = mapped_column(String(32), nullable=False, default="default")
    deployment_type: Mapped[str] = mapped_column(String(16), nullable=False)
    canary_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("optimization_experiments.id", ondelete="RESTRICT")
    )
    request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deployment_requests.id", ondelete="RESTRICT")
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rolled_back_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    __table_args__ = (
        CheckConstraint(
            "deployment_type in ('initial','promotion','canary','rollback','manual_edit')", name="type_valid"
        ),
        CheckConstraint("status in ('active','superseded','rolled_back')", name="status_valid"),
        Index("ix_agent_deployments_agent", "agent_id", "deployed_at"),
    )


class RolloutEvent(Base):
    __tablename__ = "rollout_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    deployment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_deployments.id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (Index("ix_rollout_events_deployment", "deployment_id", "id"),)


class AutopilotEvent(Base):
    """Workspace-scoped ordered event log for Autopilot activity (streamed over SSE)."""

    __tablename__ = "autopilot_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=EMPTY_OBJECT, nullable=False)
    created_at: Mapped[datetime] = created_at_col()
    __table_args__ = (UniqueConstraint("workspace_id", "sequence_number", name="uq_autopilot_events_ws_seq"),)
