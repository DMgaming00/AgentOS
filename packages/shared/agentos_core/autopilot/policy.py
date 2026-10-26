"""Autopilot policy: what the optimizer may change, deployment gates, cost and monitoring limits.

Stored per workspace in ``workspaces.settings["autopilot"]`` and validated here. Some
protections are deliberately *not* configurable (see ``NEVER_OPTIMIZABLE``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# Fields an optimizer may ever touch (subset further restricted by policy.optimizable_fields).
OPTIMIZABLE_FIELDS = (
    "system_instructions",
    "tool_descriptions",
    "tool_strategy",
    "allowed_tool_ids",
    "model",
    "provider",
    "model_configuration.effort",
    "model_configuration.max_tokens",
    "model_configuration.temperature",
    "max_steps",
    "retry_max_attempts",
)
# Never changeable by a candidate, regardless of policy: budget ceilings, timeouts, scenario wiring,
# and anything outside the agent config (approvals, permissions, observability, eval data).
NEVER_OPTIMIZABLE = ("max_cost_usd", "timeout_seconds", "schema_version")
# Providers whose current models reject sampling parameters (temperature/top_p/top_k).
NO_SAMPLING_PARAMS = {"anthropic", "openai"}


class AllowedModel(BaseModel):
    provider: str
    model: str


class Gates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_validation_cases: int = Field(default=10, ge=1)
    max_regressions: int = Field(default=0, ge=0)
    noninferiority_margin: float = Field(default=0.0, ge=0, le=1, description="allowed drop in success rate")
    min_candidate_success_rate: float = Field(default=0.5, ge=0, le=1)
    max_cost_per_success_increase: float = Field(default=0.10, ge=0, description="relative to baseline")
    max_cost_per_success_usd: float = Field(default=1.0, ge=0)
    max_incomplete_rate: float = Field(default=0.0, ge=0, le=1)
    no_regression_tags: list[str] = Field(default_factory=lambda: ["critical"])


class Monitoring(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min_live_samples: int = Field(default=5, ge=1)
    failure_rate_increase_threshold: float = Field(default=0.25, ge=0, le=1)
    auto_rollback: bool = False  # off by default: rollback is a human decision


class AutopilotPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    optimizable_fields: list[str] = Field(default_factory=lambda: list(OPTIMIZABLE_FIELDS))
    allowed_models: list[AllowedModel] = Field(default_factory=list)
    max_steps_limit: int = Field(default=12, ge=1, le=50)
    max_candidates: int = Field(default=3, ge=1, le=5)
    max_cycle_spend_usd: float = Field(default=2.0, ge=0)
    max_eval_trials: int = Field(default=3, ge=1, le=10)
    max_eval_concurrency: int = Field(default=4, ge=1, le=32)
    paid_confirmation_threshold_usd: float = Field(default=1.0, ge=0)
    per_case_max_cost_usd: float = Field(default=0.25, ge=0)
    approval_ttl_hours: int = Field(default=48, ge=1, le=720)
    diagnosis_provider: str = "mock"
    optimizer_provider: str = "mock"
    gates: Gates = Field(default_factory=Gates)
    monitoring: Monitoring = Field(default_factory=Monitoring)

    def allows_model(self, provider: str, model: str) -> bool:
        return any(m.provider == provider and m.model == model for m in self.allowed_models)


def load_policy(settings: dict[str, Any] | None) -> AutopilotPolicy:
    raw = (settings or {}).get("autopilot") or {}
    policy = AutopilotPolicy.model_validate(raw)
    # Unknown or never-optimizable fields in stored policy are dropped, not honored.
    policy.optimizable_fields = [f for f in policy.optimizable_fields if f in OPTIMIZABLE_FIELDS]
    return policy
