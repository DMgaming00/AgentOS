"""Fault catalog and the validated experiment plan.

Every fault is injected at the boundary where production code meets the outside world (provider adapter call,
tool execution, worker process, Redis, PostgreSQL connection, run budget, approval decision), so the platform's
real retry, policy, fencing and recovery logic is what responds. Nothing here "pretends" an error message.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class FaultType(StrEnum):
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    MALFORMED_MODEL_RESPONSE = "malformed_model_response"
    CORRUPTED_FIXTURE_RESPONSE = "corrupted_fixture_response"
    CONTEXT_OVERFLOW = "context_overflow"
    INVALID_TOOL_ARGUMENTS = "invalid_tool_arguments"
    TOOL_TIMEOUT = "tool_timeout"
    TOOL_FAILURE = "tool_failure"
    MALICIOUS_TOOL_RESULT = "malicious_tool_result"
    DUPLICATE_DELIVERY = "duplicate_delivery"
    WORKER_CRASH = "worker_crash"
    REDIS_INTERRUPTION = "redis_interruption"
    POSTGRES_INTERRUPTION = "postgres_interruption"
    DELAYED_NOTIFICATIONS = "delayed_notifications"
    APPROVAL_DELAY = "approval_delay"
    APPROVAL_REJECTION = "approval_rejection"
    BUDGET_EXHAUSTION = "budget_exhaustion"


class Point(StrEnum):
    STEP_START = "step_start"
    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_CALL = "after_model_call"
    BEFORE_TOOL = "before_tool"
    AFTER_TOOL = "after_tool"
    AFTER_COMPLETE = "after_complete"
    RUN_CREATION = "run_creation"
    APPROVAL = "approval"


# fault -> (allowed points, default point, one-line description, what the platform is expected to do)
CATALOG: dict[FaultType, dict[str, Any]] = {
    FaultType.PROVIDER_TIMEOUT: {
        "points": [Point.BEFORE_MODEL_CALL],
        "default": Point.BEFORE_MODEL_CALL,
        "what": "The model provider call times out (ProviderTimeoutError)",
        "expect": "transient: step retried with backoff",
    },
    FaultType.PROVIDER_RATE_LIMIT: {
        "points": [Point.BEFORE_MODEL_CALL],
        "default": Point.BEFORE_MODEL_CALL,
        "what": "HTTP 429 from the provider (ProviderRateLimitError)",
        "expect": "transient: step retried with backoff",
    },
    FaultType.MALFORMED_MODEL_RESPONSE: {
        "points": [Point.AFTER_MODEL_CALL],
        "default": Point.AFTER_MODEL_CALL,
        "what": "The model returns truncated, non-conforming output",
        "expect": "output-contract validation fails, one repair turn",
    },
    FaultType.CORRUPTED_FIXTURE_RESPONSE: {
        "points": [Point.AFTER_MODEL_CALL],
        "default": Point.AFTER_MODEL_CALL,
        "what": "A stored fixture/recorded response is corrupted (garbage text, mangled tool arguments)",
        "expect": "validation rejects it; repair or fail visibly",
    },
    FaultType.CONTEXT_OVERFLOW: {
        "points": [Point.BEFORE_MODEL_CALL],
        "default": Point.BEFORE_MODEL_CALL,
        "what": "Provider rejects the request as too long (context_length_exceeded)",
        "expect": "permanent: step fails with a clear error, no retry storm",
    },
    FaultType.INVALID_TOOL_ARGUMENTS: {
        "points": [Point.BEFORE_TOOL],
        "default": Point.BEFORE_TOOL,
        "what": "The model's tool arguments arrive corrupted (wrong types)",
        "expect": "schema validation rejects; the error is returned to the model",
    },
    FaultType.TOOL_TIMEOUT: {
        "points": [Point.BEFORE_TOOL],
        "default": Point.BEFORE_TOOL,
        "what": "The tool does not return within its timeout",
        "expect": "tool execution times out and is recorded failed",
    },
    FaultType.TOOL_FAILURE: {
        "points": [Point.BEFORE_TOOL],
        "default": Point.BEFORE_TOOL,
        "what": "The tool raises an execution error",
        "expect": "failure recorded and returned to the model",
    },
    FaultType.MALICIOUS_TOOL_RESULT: {
        "points": [Point.AFTER_TOOL],
        "default": Point.AFTER_TOOL,
        "what": "A tool result carries injected instructions (attempting an unauthorized tool call)",
        "expect": "policy denies any unauthorized call; no side effect",
    },
    FaultType.DUPLICATE_DELIVERY: {
        "points": [Point.AFTER_TOOL, Point.AFTER_COMPLETE],
        "default": Point.AFTER_TOOL,
        "what": "The same tool call / step completion is delivered a second time",
        "expect": "idempotency key replays the cached result; fenced duplicate completion is rejected",
    },
    FaultType.WORKER_CRASH: {
        "points": [Point.STEP_START, Point.BEFORE_MODEL_CALL, Point.AFTER_TOOL],
        "default": Point.BEFORE_MODEL_CALL,
        "what": "The worker process is killed (exit 137) with the lease held",
        "expect": "lease expires, another worker resumes from the checkpoint",
    },
    FaultType.REDIS_INTERRUPTION: {
        "points": [Point.STEP_START],
        "default": Point.STEP_START,
        "what": "Redis connections are dropped and refused for a window",
        "expect": "wake-ups degrade to polling; no state lost",
    },
    FaultType.POSTGRES_INTERRUPTION: {
        "points": [Point.STEP_START, Point.BEFORE_TOOL],
        "default": Point.STEP_START,
        "what": "This worker's PostgreSQL connections are terminated",
        "expect": "transactions roll back; pool reconnects; step retried or recovered",
    },
    FaultType.DELAYED_NOTIFICATIONS: {
        "points": [Point.STEP_START],
        "default": Point.STEP_START,
        "what": "Redis notifications are delayed and arrive out of order",
        "expect": "consumers re-read PostgreSQL; ordering of events unaffected",
    },
    FaultType.APPROVAL_DELAY: {
        "points": [Point.APPROVAL],
        "default": Point.APPROVAL,
        "what": "The (simulated) reviewer takes a long time to decide",
        "expect": "run waits durably; completes after the decision",
    },
    FaultType.APPROVAL_REJECTION: {
        "points": [Point.APPROVAL],
        "default": Point.APPROVAL,
        "what": "The (simulated) reviewer rejects the action",
        "expect": "the action is never executed; run fails or recovers per workflow",
    },
    FaultType.BUDGET_EXHAUSTION: {
        "points": [Point.RUN_CREATION],
        "default": Point.RUN_CREATION,
        "what": "The run's budget is cut to near zero",
        "expect": "BudgetExceeded stops the run before overspending",
    },
}

# Faults that kill or disturb a whole worker process (and so may affect other concurrent steps on it).
PROCESS_LEVEL = {
    FaultType.WORKER_CRASH,
    FaultType.REDIS_INTERRUPTION,
    FaultType.POSTGRES_INTERRUPTION,
    FaultType.DELAYED_NOTIFICATIONS,
}
MAX_RUNS = 40
MAX_DURATION_SECONDS = 600


class FaultSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: FaultType
    point: Point | None = None
    probability: float = Field(default=0.3, ge=0, le=1)
    # Optional narrowing: only this step node / tool name.
    node_key: str | None = Field(default=None, max_length=100)
    tool: str | None = Field(default=None, max_length=100)
    # Fault parameters, e.g. {"delay_seconds": 5} for approval_delay, {"window_seconds": 3} for redis_interruption.
    params: dict[str, float | int | str] = Field(default_factory=dict)
    # Per step, not per run: concurrent steps of one run would otherwise race for a shared budget and make the
    # schedule depend on timing. Attempts of one step are sequential, so a per-step cap is deterministic.
    max_per_step: int = Field(default=1, ge=1, le=5, description="fire at most this many times in one step")

    @model_validator(mode="after")
    def _check(self) -> FaultSpec:
        meta = CATALOG[self.type]
        if self.point is None:
            self.point = meta["default"]
        if self.point not in meta["points"]:
            raise ValueError(
                f"{self.type.value} cannot be injected at {self.point.value}; allowed: "
                f"{[p.value for p in meta['points']]}"
            )
        return self


class ChaosPlan(BaseModel):
    """A reproducible fault schedule: the same plan + seed produces the same decisions for the same cases."""

    model_config = ConfigDict(extra="forbid")
    seed: int = Field(default=1, ge=0, le=2**31 - 1)
    faults: list[FaultSpec] = Field(min_length=1, max_length=8)
    trials: int = Field(default=10, ge=1, le=MAX_RUNS, description="runs (cases) in the experiment")
    max_affected_runs: int = Field(default=10, ge=1, le=MAX_RUNS)
    duration_seconds: int = Field(default=120, ge=10, le=MAX_DURATION_SECONDS)
    # as_configured: the workflow's own retry settings; no_retry: every step gets one attempt; extra_retries: 5 attempts.
    recovery_policy: str = Field(default="as_configured", pattern="^(as_configured|no_retry|extra_retries)$")
    # Simulated reviewer for approval gates in experiment runs (labeled as such in results).
    approver: str = Field(default="auto_approve", pattern="^(auto_approve|none)$")
    safety_budget_usd: float = Field(default=1.0, ge=0, le=25)
    run_input: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check(self) -> ChaosPlan:
        if self.max_affected_runs > self.trials:
            self.max_affected_runs = self.trials
        return self

    def affected_cases(self) -> frozenset[int]:
        """The cases (runs) faults may touch: the ``max_affected_runs`` cases with the lowest hash-based rank.

        Chosen up front from the seed alone, so the same plan affects the same cases in every experiment (and a
        before/after comparison faces the same schedule) instead of "whichever runs happen to fail first".
        """
        import hashlib

        def rank(i: int) -> int:
            return int.from_bytes(hashlib.sha256(f"{self.seed}|case-rank|{i}".encode()).digest()[:8], "big")

        return frozenset(sorted(range(self.trials), key=rank)[: self.max_affected_runs])


PRESETS: dict[str, dict[str, Any]] = {
    "provider-brownout": {
        "description": "30% of model calls time out or are rate limited: do retries absorb a flaky provider?",
        "plan": {
            "seed": 7,
            "trials": 10,
            "max_affected_runs": 10,
            "faults": [
                {"type": "provider_timeout", "probability": 0.25},
                {"type": "provider_rate_limit", "probability": 0.25},
            ],
        },
    },
    "worker-crash-loop": {
        "description": "Workers die mid-step: leases expire and work resumes from checkpoints.",
        "plan": {
            "seed": 11,
            "trials": 6,
            "max_affected_runs": 6,
            "duration_seconds": 180,
            "faults": [{"type": "worker_crash", "point": "before_model_call", "probability": 0.35}],
        },
    },
    "garbage-model-output": {
        "description": "Truncated and corrupted model output: does validation catch it and the repair turn recover?",
        "plan": {
            "seed": 3,
            "trials": 10,
            "max_affected_runs": 10,
            "faults": [
                {"type": "malformed_model_response", "probability": 0.4},
                {"type": "corrupted_fixture_response", "probability": 0.2},
            ],
        },
    },
    "flaky-tools": {
        "description": "Tools time out, fail, or receive corrupted arguments.",
        "plan": {
            "seed": 5,
            "trials": 10,
            "max_affected_runs": 10,
            "faults": [
                {"type": "tool_failure", "probability": 0.3},
                {"type": "invalid_tool_arguments", "probability": 0.3},
                {"type": "tool_timeout", "probability": 0.1},
            ],
        },
    },
    "duplicate-delivery": {
        "description": "Tool calls and step completions are delivered twice: no duplicated side effect.",
        "plan": {
            "seed": 9,
            "trials": 8,
            "max_affected_runs": 8,
            "faults": [
                {"type": "duplicate_delivery", "point": "after_tool", "probability": 0.6, "max_per_step": 3},
                {
                    "type": "duplicate_delivery",
                    "point": "after_complete",
                    "probability": 0.4,
                    "max_per_step": 3,
                },
            ],
        },
    },
    "infrastructure-hiccups": {
        "description": "Redis and PostgreSQL connections drop and notifications arrive late and out of order.",
        "plan": {
            "seed": 13,
            "trials": 8,
            "max_affected_runs": 8,
            "faults": [
                {"type": "redis_interruption", "probability": 0.5, "params": {"window_seconds": 2}},
                {"type": "delayed_notifications", "probability": 0.5, "params": {"max_delay_ms": 400}},
                {"type": "postgres_interruption", "probability": 0.3},
            ],
        },
    },
    "hostile-tool-output": {
        "description": "A tool result contains injected instructions: policy must hold.",
        "plan": {
            "seed": 17,
            "trials": 8,
            "max_affected_runs": 8,
            "faults": [{"type": "malicious_tool_result", "probability": 0.8, "max_per_step": 2}],
        },
    },
}
