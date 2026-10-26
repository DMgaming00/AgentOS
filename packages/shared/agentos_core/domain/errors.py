"""Error taxonomy used for retry classification.

``TransientError`` subclasses are retried with backoff; everything else is a
permanent failure that fails the step immediately.
"""

from __future__ import annotations

from typing import Any


class AgentOSError(Exception):
    error_type = "internal_error"
    retryable = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.error_type, "message": self.message, "retryable": self.retryable, **self.details}


class TransientError(AgentOSError):
    error_type = "transient_error"
    retryable = True


class PermanentError(AgentOSError):
    error_type = "permanent_error"


# --- transient ---------------------------------------------------------------
class ProviderRateLimitError(TransientError):
    error_type = "provider_rate_limited"


class ProviderTimeoutError(TransientError):
    error_type = "provider_timeout"


class ProviderUnavailableError(TransientError):
    error_type = "provider_unavailable"


class WorkerLostError(TransientError):
    error_type = "worker_lost"


class StepTimeoutError(TransientError):
    error_type = "step_timeout"


# --- permanent ---------------------------------------------------------------
class InvalidInputError(PermanentError):
    error_type = "invalid_input"


class PolicyViolationError(PermanentError):
    error_type = "policy_violation"


class ToolNotAllowedError(PolicyViolationError):
    error_type = "tool_not_allowed"


class BudgetExceededError(PermanentError):
    error_type = "budget_exceeded"


class ProviderAuthError(PermanentError):
    error_type = "provider_auth_error"


class ProviderRequestError(PermanentError):
    error_type = "provider_bad_request"


class ProviderNotConfiguredError(PermanentError):
    error_type = "provider_not_configured"


class InvalidModelOutputError(PermanentError):
    error_type = "invalid_model_output"


class ApprovalRejectedError(PermanentError):
    error_type = "approval_rejected"


class StepLimitExceededError(PermanentError):
    error_type = "step_limit_exceeded"


class ToolExecutionError(PermanentError):
    error_type = "tool_error"


class LeaseLostError(Exception):
    """Raised inside a worker when its lease was revoked (fencing)."""


class RunCancelledError(Exception):
    """Raised inside a worker when the run was cancelled."""


def classify(exc: BaseException) -> AgentOSError:
    """Normalize any exception into the taxonomy. Unknown errors are permanent."""
    if isinstance(exc, AgentOSError):
        return exc
    if isinstance(exc, TimeoutError):
        return StepTimeoutError("operation timed out")
    return AgentOSError(f"{type(exc).__name__}: {exc}")
