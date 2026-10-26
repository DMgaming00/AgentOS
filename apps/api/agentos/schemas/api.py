"""Request/response schemas. These drive the OpenAPI document and the generated TS client."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

T = TypeVar("T")


class ORM(BaseModel):
    # Fields with defaults are always present in responses, so mark them required in the schema.
    model_config = ConfigDict(from_attributes=True, json_schema_serialization_defaults_required=True)


class Page[T](BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    items: list[T]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


# ----------------------------------------------------------------------------- auth


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=200)


class Membership(BaseModel):
    workspace_id: uuid.UUID
    workspace_name: str
    role: str


class UserOut(ORM):
    id: uuid.UUID
    email: str
    role: str


class MeResponse(BaseModel):
    user: UserOut
    memberships: list[Membership]


class TokenResponse(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserOut
    memberships: list[Membership]


# ----------------------------------------------------------------------------- agents

Provider = Literal["anthropic", "openai", "mock", "fixture"]


class AgentBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(default="", max_length=2000)
    system_instructions: str = Field(default="", max_length=20_000)
    provider: Provider = "mock"
    model: str = Field(min_length=1, max_length=200)
    model_configuration: dict[str, Any] = Field(default_factory=dict)
    allowed_tool_ids: list[str] = Field(default_factory=list, max_length=20)
    max_steps: int = Field(default=8, ge=1, le=50)
    max_cost_usd: Decimal = Field(default=Decimal("1"), ge=0, le=100)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)


class AgentCreate(AgentBase):
    name: str = Field(min_length=1, max_length=200)


class AgentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    system_instructions: str | None = Field(default=None, max_length=20_000)
    provider: Provider | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    model_configuration: dict[str, Any] | None = None
    allowed_tool_ids: list[str] | None = Field(default=None, max_length=20)
    max_steps: int | None = Field(default=None, ge=1, le=50)
    max_cost_usd: Decimal | None = Field(default=None, ge=0, le=100)
    timeout_seconds: int | None = Field(default=None, ge=1, le=3600)


class AgentOut(ORM):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str
    system_instructions: str
    provider: str
    model: str
    model_configuration: dict[str, Any]
    allowed_tool_ids: list[str]
    max_steps: int
    max_cost_usd: Decimal
    timeout_seconds: int
    active_version_id: uuid.UUID | None = None
    canary_version_id: uuid.UUID | None = None
    canary_percent: int = 0
    created_at: datetime
    updated_at: datetime


class AgentVersionOut(ORM):
    id: uuid.UUID
    agent_id: uuid.UUID
    version: int
    parent_version_id: uuid.UUID | None
    config_snapshot: dict[str, Any]
    config_hash: str
    source: str
    change_summary: str
    created_by: uuid.UUID | None
    created_at: datetime
    is_active: bool = False
    is_canary: bool = False


class AgentExecution(BaseModel):
    run_id: uuid.UUID
    step_id: uuid.UUID
    node_key: str
    status: str
    attempt_count: int
    started_at: datetime | None
    completed_at: datetime | None
    workflow_name: str


# ----------------------------------------------------------------------------- workflows


class ValidationIssueOut(BaseModel):
    code: str
    message: str
    node: str | None = None


class ValidateRequest(BaseModel):
    definition: dict[str, Any]


class ValidateResponse(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    valid: bool
    issues: list[ValidationIssueOut]
    topological_order: list[str]
    levels: dict[str, int]
    normalized: dict[str, Any] | None = None


class WorkflowCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
    description: str = Field(default="", max_length=2000)
    definition: dict[str, Any]


class WorkflowOut(ORM):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    description: str
    version: int
    definition: dict[str, Any]
    created_at: datetime


class WorkflowSummary(WorkflowOut):
    version_count: int = 1
    node_count: int = 0
    last_run_status: str | None = None
    last_run_at: datetime | None = None


class WorkflowDetail(WorkflowOut):
    validation: ValidateResponse
    versions: list[int]


class PlanRequest(BaseModel):
    task: str = Field(min_length=3, max_length=4000)
    provider: Literal["mock", "anthropic", "openai"] | None = None
    scenario: str | None = Field(
        default=None, max_length=50, description="mock provider scenario (tests/demos)"
    )


class PlanAttemptOut(BaseModel):
    issues: list[ValidationIssueOut]
    usage: dict[str, Any]


class PlanResponse(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    valid: bool
    definition: dict[str, Any] | None
    issues: list[ValidationIssueOut]
    levels: dict[str, int]
    attempts: list[PlanAttemptOut]
    provider: str
    model: str
    simulated: bool


class RunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict[str, Any] = Field(default_factory=dict)


class PlannedRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: str = Field(min_length=3, max_length=4000)
    input: dict[str, Any] = Field(default_factory=dict)
    max_cost_usd: float = Field(default=2.0, ge=0, le=100)


# ----------------------------------------------------------------------------- runs


class RunOut(ORM):
    id: uuid.UUID
    workspace_id: uuid.UUID
    workflow_definition_id: uuid.UUID
    workflow_version: int
    workflow_name: str | None = None
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    requested_by: uuid.UUID | None
    budget: dict[str, Any]
    model_calls: int
    tool_calls: int
    cost_spent_usd: Decimal
    cost_reserved_usd: Decimal
    event_seq: int
    trace_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime
    step_counts: dict[str, int] = Field(default_factory=dict)
    duration_ms: int | None = None


class AttemptOut(ORM):
    id: uuid.UUID
    attempt_number: int
    worker_id: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    queue_wait_ms: int | None
    error_type: str | None
    error_message: str | None
    execution_metadata: dict[str, Any]


class StepOut(ORM):
    id: uuid.UUID
    run_id: uuid.UUID
    node_key: str
    node_type: str
    node_config: dict[str, Any]
    agent_definition_id: uuid.UUID | None
    agent_version_id: uuid.UUID | None = None
    agent_name: str | None = None
    dependency_keys: list[str]
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    error: dict[str, Any] | None
    attempt_count: int
    failure_count: int
    max_attempts: int
    next_run_at: datetime | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    ready_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    attempts: list[AttemptOut] = Field(default_factory=list)

    @field_validator("dependency_keys", mode="before")
    @classmethod
    def _deps(cls, v: Any) -> Any:
        return list(v or [])


class ToolExecutionOut(ORM):
    id: uuid.UUID
    step_id: uuid.UUID
    attempt_id: uuid.UUID | None
    tool_call_id: str | None
    tool_name: str
    arguments: dict[str, Any]
    status: str
    result: dict[str, Any] | None
    requires_approval: bool
    started_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    error: dict[str, Any] | None


class ModelUsageOut(ORM):
    id: uuid.UUID
    step_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
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
    status: str
    error_type: str | None
    recorded_at: datetime


class EventOut(ORM):
    id: int
    run_id: uuid.UUID
    step_id: uuid.UUID | None
    sequence_number: int
    event_type: str
    timestamp: datetime
    payload: dict[str, Any]
    trace_id: str | None


class ApprovalOut(ORM):
    id: uuid.UUID
    run_id: uuid.UUID
    step_id: uuid.UUID
    node_key: str | None = None
    workflow_name: str | None = None
    tool_execution_id: uuid.UUID | None
    tool_name: str | None = None
    requested_action: str
    reason: str | None
    affected_resource: str | None
    requesting_agent: str | None
    sanitized_arguments: dict[str, Any]
    arguments_hash: str
    status: str
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decided_by: uuid.UUID | None
    decided_by_email: str | None = None
    decision_reason: str | None
    consumed_at: datetime | None


class StepDetail(StepOut):
    tool_executions: list[ToolExecutionOut]
    model_usage: list[ModelUsageOut]
    events: list[EventOut]
    approvals: list[ApprovalOut]


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=2000)


class ArtifactOut(ORM):
    id: uuid.UUID
    run_id: uuid.UUID
    workspace_id: uuid.UUID
    step_id: uuid.UUID | None
    filename: str
    content_type: str
    sha256: str
    byte_size: int
    created_at: datetime


class DocumentOut(ORM):
    id: uuid.UUID
    filename: str
    title: str
    content_type: str
    sha256: str
    byte_size: int
    created_at: datetime
    chunk_count: int = 0


class ToolOut(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    name: str
    description: str
    input_schema: dict[str, Any]
    result_schema: dict[str, Any] | None
    permission: str
    side_effect: str
    timeout_seconds: float
    idempotent: bool
    requires_approval: bool
    enabled: bool


class WorkspaceOut(ORM):
    id: uuid.UUID
    name: str
    settings: dict[str, Any]
    created_at: datetime
    role: str | None = None
    members: list[dict[str, Any]] = Field(default_factory=list)


class WorkspaceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=200)
    max_run_cost_usd: float | None = Field(default=None, ge=0, le=1000)


class SystemInfo(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    version: str
    environment: str
    providers: dict[str, dict[str, Any]]
    execution: dict[str, Any]
    planner_provider: str
    dev_auth_enabled: bool
    otel_enabled: bool
    redis_available: bool
    pricing: dict[str, Any]
    limits: dict[str, Any]
    engine: dict[str, Any]
