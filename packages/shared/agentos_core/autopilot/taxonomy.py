"""Normalized failure taxonomy.

Classification is deterministic: it maps recorded error types (from attempts, step
errors, tool executions, model usage and evaluator results) to a category, and
marks whether the failure is infrastructure (not the agent's reasoning). The model
is never assumed to be at fault by default.
"""

from __future__ import annotations

from enum import StrEnum


class FailureCategory(StrEnum):
    TOOL_ARGUMENT_ERROR = "TOOL_ARGUMENT_ERROR"
    TOOL_PERMISSION_DENIED = "TOOL_PERMISSION_DENIED"
    TOOL_EXECUTION_FAILURE = "TOOL_EXECUTION_FAILURE"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_RATE_LIMIT = "MODEL_RATE_LIMIT"
    MODEL_PROVIDER_ERROR = "MODEL_PROVIDER_ERROR"
    CONTEXT_OVERFLOW = "CONTEXT_OVERFLOW"
    OUTPUT_SCHEMA_INVALID = "OUTPUT_SCHEMA_INVALID"
    RETRIEVAL_FAILURE = "RETRIEVAL_FAILURE"
    FACTUAL_GROUNDING_FAILURE = "FACTUAL_GROUNDING_FAILURE"
    TASK_INCOMPLETE = "TASK_INCOMPLETE"
    REPETITIVE_TOOL_LOOP = "REPETITIVE_TOOL_LOOP"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    WORKER_FAILURE = "WORKER_FAILURE"
    APPROVAL_REJECTED = "APPROVAL_REJECTED"
    COST_ANOMALY = "COST_ANOMALY"
    LATENCY_ANOMALY = "LATENCY_ANOMALY"
    EXCESSIVE_RETRIES = "EXCESSIVE_RETRIES"
    UNKNOWN = "UNKNOWN"


# Infrastructure: the platform, provider or environment failed - not the agent's reasoning.
INFRASTRUCTURE = frozenset(
    {
        FailureCategory.MODEL_TIMEOUT,
        FailureCategory.MODEL_RATE_LIMIT,
        FailureCategory.MODEL_PROVIDER_ERROR,
        FailureCategory.WORKER_FAILURE,
        FailureCategory.TOOL_EXECUTION_FAILURE,
    }
)

# Engine/provider error types -> category.
ERROR_TYPE_MAP: dict[str, FailureCategory] = {
    "provider_timeout": FailureCategory.MODEL_TIMEOUT,
    "step_timeout": FailureCategory.MODEL_TIMEOUT,
    "provider_rate_limited": FailureCategory.MODEL_RATE_LIMIT,
    "provider_unavailable": FailureCategory.MODEL_PROVIDER_ERROR,
    "provider_auth_error": FailureCategory.MODEL_PROVIDER_ERROR,
    "provider_not_configured": FailureCategory.MODEL_PROVIDER_ERROR,
    "provider_bad_request": FailureCategory.MODEL_PROVIDER_ERROR,
    "model_refusal": FailureCategory.TASK_INCOMPLETE,
    "worker_lost": FailureCategory.WORKER_FAILURE,
    "invalid_model_output": FailureCategory.OUTPUT_SCHEMA_INVALID,
    "invalid_tool_arguments": FailureCategory.TOOL_ARGUMENT_ERROR,
    "tool_not_allowed": FailureCategory.TOOL_PERMISSION_DENIED,
    "policy_violation": FailureCategory.TOOL_PERMISSION_DENIED,
    "tool_error": FailureCategory.TOOL_EXECUTION_FAILURE,
    "budget_exceeded": FailureCategory.BUDGET_EXCEEDED,
    "step_limit_exceeded": FailureCategory.TASK_INCOMPLETE,
    "approval_rejected": FailureCategory.APPROVAL_REJECTED,
    "invalid_input": FailureCategory.TASK_INCOMPLETE,
}

# OpenEval method failures -> category (used for evaluation-detected failures).
EVALUATOR_MAP: dict[str, FailureCategory] = {
    "schema_validation": FailureCategory.OUTPUT_SCHEMA_INVALID,
    "numeric_tolerance": FailureCategory.FACTUAL_GROUNDING_FAILURE,
    "exact_match": FailureCategory.FACTUAL_GROUNDING_FAILURE,
    "source_citation_check": FailureCategory.FACTUAL_GROUNDING_FAILURE,
    "tool_correctness": FailureCategory.RETRIEVAL_FAILURE,
    "task_completion": FailureCategory.TASK_INCOMPLETE,
}


def classify_error_type(error_type: str | None, message: str = "") -> FailureCategory:
    if not error_type:
        return FailureCategory.UNKNOWN
    if error_type == "invalid_model_output" and "max_tokens" in message:
        return FailureCategory.CONTEXT_OVERFLOW
    if error_type in ("provider_bad_request",) and (
        "context" in message.lower() or "too long" in message.lower()
    ):
        return FailureCategory.CONTEXT_OVERFLOW
    return ERROR_TYPE_MAP.get(error_type, FailureCategory.UNKNOWN)


def is_infrastructure(category: FailureCategory | str) -> bool:
    return FailureCategory(category) in INFRASTRUCTURE
