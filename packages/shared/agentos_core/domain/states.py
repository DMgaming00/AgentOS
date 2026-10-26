"""Workflow, step and approval state machines.

Every status change in the engine goes through ``assert_*_transition`` so the
legal transitions are defined in exactly one place.
"""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    PLANNING = "planning"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class StepStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SUSPENDED = "suspended"  # paused for human approval
    LEASE_EXPIRED = "lease_expired"  # worker lost; reclaimed by recovery
    STALE = "stale"  # finished after its lease was taken over; result discarded
    CANCELLED = "cancelled"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ToolExecutionStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    DENIED = "denied"  # blocked by policy, never executed


class Role(StrEnum):
    ADMIN = "admin"
    DEVELOPER = "developer"
    VIEWER = "viewer"


TERMINAL_RUN = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})
TERMINAL_STEP = frozenset({StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.SKIPPED, StepStatus.CANCELLED})
ACTIVE_STEP = frozenset({StepStatus.RUNNING, StepStatus.WAITING_APPROVAL})

RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset(
        {RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    RunStatus.PLANNING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.WAITING_APPROVAL, RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.SUCCEEDED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: dict[StepStatus, frozenset[StepStatus]] = {
    StepStatus.PENDING: frozenset({StepStatus.READY, StepStatus.SKIPPED, StepStatus.CANCELLED}),
    StepStatus.READY: frozenset({StepStatus.RUNNING, StepStatus.SKIPPED, StepStatus.CANCELLED}),
    StepStatus.RUNNING: frozenset(
        {
            StepStatus.SUCCEEDED,
            StepStatus.FAILED,
            StepStatus.RETRY_WAIT,
            StepStatus.WAITING_APPROVAL,
            StepStatus.CANCELLED,
        }
    ),
    StepStatus.WAITING_APPROVAL: frozenset(
        {StepStatus.READY, StepStatus.SUCCEEDED, StepStatus.FAILED, StepStatus.CANCELLED}
    ),
    StepStatus.RETRY_WAIT: frozenset({StepStatus.READY, StepStatus.CANCELLED, StepStatus.SKIPPED}),
    StepStatus.SUCCEEDED: frozenset(),
    StepStatus.FAILED: frozenset(),
    StepStatus.SKIPPED: frozenset(),
    StepStatus.CANCELLED: frozenset(),
}

APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset(
        {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}
    ),
    ApprovalStatus.APPROVED: frozenset(),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
}


class IllegalTransitionError(ValueError):
    def __init__(self, kind: str, current: str, target: str) -> None:
        super().__init__(f"illegal {kind} transition {current!r} -> {target!r}")
        self.kind, self.current, self.target = kind, current, target


def can_transition_run(current: RunStatus | str, target: RunStatus | str) -> bool:
    return RunStatus(target) in RUN_TRANSITIONS[RunStatus(current)]


def assert_run_transition(current: RunStatus | str, target: RunStatus | str) -> None:
    if not can_transition_run(current, target):
        raise IllegalTransitionError("run", str(current), str(target))


def can_transition_step(current: StepStatus | str, target: StepStatus | str) -> bool:
    return StepStatus(target) in STEP_TRANSITIONS[StepStatus(current)]


def assert_step_transition(current: StepStatus | str, target: StepStatus | str) -> None:
    if not can_transition_step(current, target):
        raise IllegalTransitionError("step", str(current), str(target))


def assert_approval_transition(current: ApprovalStatus | str, target: ApprovalStatus | str) -> None:
    if ApprovalStatus(target) not in APPROVAL_TRANSITIONS[ApprovalStatus(current)]:
        raise IllegalTransitionError("approval", str(current), str(target))
