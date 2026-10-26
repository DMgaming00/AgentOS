"""Autopilot request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agentos.schemas.api import ORM


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID | None = None
    window_hours: int = Field(default=168, ge=1, le=24 * 90)


class DiagnoseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    category: str = Field(max_length=48)
    provider: Literal["mock", "anthropic", "openai"] | None = None


class SignalOut(ORM):
    id: uuid.UUID
    agent_id: uuid.UUID | None
    agent_version_id: uuid.UUID | None
    run_id: uuid.UUID
    step_id: uuid.UUID | None
    category: str
    signal_type: str
    detector: str
    is_infrastructure: bool
    summary: str
    evidence: dict[str, Any]
    measurements: dict[str, Any]
    cost_usd: float | None
    occurred_at: datetime
    detected_at: datetime


class DiagnosisOut(ORM):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID | None
    run_id: uuid.UUID | None
    failure_category: str
    signal_ids: list[Any]
    evidence_refs: list[Any]
    hypothesis: str
    confidence: str
    proposed_remedy: str
    limitations: list[Any]
    status: str
    validation_errors: list[Any]
    provider: str
    model: str
    simulated: bool
    created_at: datetime
    label: str = "hypothesis (not a verified root cause)"


class DiagnosisDetail(DiagnosisOut):
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class FailuresOut(BaseModel):
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)
    window_hours: int
    clusters: list[dict[str, Any]]
    signals: list[SignalOut]
    diagnoses: list[DiagnosisOut]
    timeline: list[dict[str, Any]]
    top_tools: list[dict[str, Any]]
    suggested_investigations: list[dict[str, Any]]


class GenerateCandidatesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    diagnosis_id: uuid.UUID | None = None
    count: int | None = Field(default=None, ge=1, le=5)
    provider: Literal["mock", "anthropic", "openai"] | None = None


class ChangeIn(BaseModel):
    field: str = Field(max_length=100)
    value: Any = None
    reason: str = Field(default="", max_length=2000)


class ManualCandidateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    title: str = Field(min_length=1, max_length=300)
    changes: list[ChangeIn] = Field(min_length=1, max_length=10)
    expected_benefits: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class CandidateOut(ORM):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str | None = None
    diagnosis_id: uuid.UUID | None
    baseline_version_id: uuid.UUID
    candidate_version_id: uuid.UUID | None
    baseline_version: int | None = None
    candidate_version: int | None = None
    title: str
    changes: list[Any]
    diff: list[Any]
    reason: str
    expected_benefits: list[Any]
    risk_assessment: list[Any]
    policy_violations: list[Any]
    status: str
    generator: str
    provider: str | None
    model: str | None
    simulated: bool
    created_at: datetime
    latest_experiment_id: uuid.UUID | None = None


class DatasetOut(ORM):
    id: uuid.UUID
    name: str
    split: str
    version: int
    visibility: str
    description: str
    dataset_hash: str
    case_count: int
    created_at: datetime


class CaseOut(BaseModel):
    case_key: str
    task: str
    input: dict[str, Any]
    expected_output: dict[str, Any] | None
    evaluation_methods: list[str]
    tags: list[Any]


class DatasetDetail(DatasetOut):
    cases: list[CaseOut]
    expected_hidden: bool = False


class EvaluationRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    dataset_id: uuid.UUID
    agent_version_id: uuid.UUID | None = None
    trials: int = Field(default=1, ge=1, le=10)
    confirm_cost: bool = False


class EvaluationRunOut(ORM):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    dataset_id: uuid.UUID
    experiment_id: uuid.UUID | None
    arm: str | None
    trials: int
    status: str
    conditions: dict[str, Any]
    metrics: dict[str, Any] | None
    max_cost_usd: float
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    progress: dict[str, int] = Field(default_factory=dict)


class ExperimentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    dataset_id: uuid.UUID
    candidate_id: uuid.UUID | None = None
    baseline_version_id: uuid.UUID | None = None
    candidate_version_id: uuid.UUID | None = None
    trials: int = Field(default=1, ge=1, le=10)
    confirm_cost: bool = False


class ExperimentOut(ORM):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str | None = None
    candidate_id: uuid.UUID | None
    baseline_version_id: uuid.UUID
    candidate_version_id: uuid.UUID
    baseline_version: int | None = None
    candidate_version: int | None = None
    validation_dataset_id: uuid.UUID
    trials: int
    status: str
    decision: str | None
    results: dict[str, Any] | None
    eligibility: dict[str, Any] | None
    conditions: dict[str, Any]
    created_at: datetime
    completed_at: datetime | None
    candidate_title: str | None = None


class PromoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    experiment_id: uuid.UUID
    mode: Literal["full", "canary"] = "full"
    canary_percent: int = Field(default=0, ge=0, le=99)
    reason: str = Field(default="", max_length=2000)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str | None = Field(default=None, max_length=2000)


class RollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: uuid.UUID
    reason: str = Field(min_length=3, max_length=2000)
    target_version_id: uuid.UUID | None = None


class DeploymentRequestOut(ORM):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str | None = None
    experiment_id: uuid.UUID
    candidate_version_id: uuid.UUID
    baseline_version_id: uuid.UUID
    candidate_version: int | None = None
    baseline_version: int | None = None
    mode: str
    canary_percent: int
    status: str
    reason: str
    requested_by: uuid.UUID | None
    requested_at: datetime
    expires_at: datetime
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    decision_reason: str | None


class DeploymentOut(ORM):
    id: uuid.UUID
    agent_id: uuid.UUID
    agent_name: str | None = None
    config_version_id: uuid.UUID
    previous_version_id: uuid.UUID | None
    version: int | None = None
    previous_version: int | None = None
    environment: str
    deployment_type: str
    canary_percent: int
    experiment_id: uuid.UUID | None
    request_id: uuid.UUID | None
    approved_by: uuid.UUID | None
    approved_by_email: str | None = None
    reason: str
    status: str
    deployed_at: datetime
    rolled_back_at: datetime | None
    rolled_back_by: uuid.UUID | None
    rollout_events: list[dict[str, Any]] = Field(default_factory=list)


class AutopilotEventOut(ORM):
    id: int
    sequence_number: int
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
