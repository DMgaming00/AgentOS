"""Coordinator duties. Every worker runs them; each is idempotent and uses
row locks with SKIP LOCKED, so concurrent coordinators do not conflict."""

from __future__ import annotations

from dataclasses import dataclass

from agentos_core.autopilot.deployment import monitoring_tick
from agentos_core.autopilot.evaluation import evaluation_tick
from agentos_core.autopilot.experiments import experiments_tick
from agentos_core.chaos.experiments import chaos_tick
from agentos_core.engine.core import (
    enforce_deadlines,
    expire_approvals,
    expire_leases,
    promote_retries,
    subflow_tick,
)
from agentos_core.log import get_logger

log = get_logger(__name__)


@dataclass
class TickResult:
    leases_expired: int = 0
    retries_promoted: int = 0
    approvals_expired: int = 0
    runs_timed_out: int = 0
    subflows_propagated: int = 0
    evaluations: dict[str, int] | None = None
    experiments_completed: int = 0
    regressions_flagged: int = 0
    chaos_finalized: int = 0


async def coordinator_tick() -> TickResult:
    r = TickResult()
    r.leases_expired = await expire_leases()
    r.retries_promoted = await promote_retries()
    r.approvals_expired = await expire_approvals()
    r.runs_timed_out = await enforce_deadlines()
    r.subflows_propagated = await subflow_tick()
    # Autopilot: schedule/grade OpenEval executions (each is an ordinary, pinned workflow run).
    r.evaluations = await evaluation_tick()
    r.experiments_completed = await experiments_tick()
    r.regressions_flagged = await monitoring_tick()
    r.chaos_finalized = await chaos_tick()
    if r.leases_expired or r.runs_timed_out or r.subflows_propagated:
        log.info("coordinator_tick", **r.__dict__)
    return r
