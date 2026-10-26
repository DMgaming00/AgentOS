"""Chaos experiments: create seeded runs, drive the simulated reviewer, enforce safety limits, finalize.

Safety (enforced here, in code):

* plans are validated and bounded (runs, duration, affected runs, per-fault caps, spend);
* faults only run for runs that belong to a *running* experiment and only while its window is open;
* the experiment is cancelled if its estimated spend exceeds ``safety_budget_usd``;
* runs still unfinished when the window closes are cancelled, never left running.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.chaos.context import claim_injection_in, roll
from agentos_core.chaos.faults import PRESETS, ChaosPlan, FaultType, Point
from agentos_core.chaos.scorecard import compute_scorecard
from agentos_core.config import get_settings
from agentos_core.db.models import (
    ApprovalRequest,
    ChaosExperiment,
    ChaosInjection,
    FaultScenario,
    Worker,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStep,
)
from agentos_core.domain.states import TERMINAL_RUN, ApprovalStatus
from agentos_core.engine import core
from agentos_core.engine.tx import run_in_transaction
from agentos_core.log import get_logger
from agentos_core.util import utcnow
from agentos_core.workflow_service import validate_for_workspace

log = get_logger(__name__)
GRACE_SECONDS = 30
RETRY_BY_POLICY = {"no_retry": 1, "extra_retries": 5}


class ChaosError(core.EngineError):
    """Chaos Lab refusals; ``not_found`` maps to 404, invalid input to 422, everything else to 409."""

    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(message, code)
        self.status_code = 404 if code == "not_found" else 422 if code.startswith("invalid") else 409


def plan_hash(plan: ChaosPlan) -> str:
    return hashlib.sha256(json.dumps(plan.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


async def ensure_presets(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    have = set(
        (
            await session.execute(
                select(FaultScenario.name).where(FaultScenario.workspace_id == workspace_id)
            )
        ).scalars()
    )
    for name, preset in PRESETS.items():
        if name not in have:
            session.add(
                FaultScenario(
                    workspace_id=workspace_id,
                    name=name,
                    description=preset["description"],
                    builtin=True,
                    plan=ChaosPlan.model_validate(preset["plan"]).model_dump(mode="json"),
                )
            )
    await session.flush()


async def create_experiment(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    name: str,
    workflow_id: uuid.UUID,
    plan: ChaosPlan,
    version_overrides: dict[uuid.UUID, uuid.UUID] | None = None,
) -> ChaosExperiment:
    if not get_settings().allow_fault_injection:
        raise ChaosError("fault injection is disabled in this environment", code="fault_injection_disabled")
    wf = await session.get(WorkflowDefinition, workflow_id)
    if wf is None or wf.workspace_id != workspace_id:
        raise ChaosError("workflow not found", code="not_found")
    result, agents = await validate_for_workspace(session, workspace_id, wf.definition)
    if not result.ok or result.spec is None:
        raise ChaosError("the target workflow does not validate", code="invalid_workflow")
    running = (
        await session.execute(
            select(func.count())
            .select_from(ChaosExperiment)
            .where(ChaosExperiment.workspace_id == workspace_id, ChaosExperiment.status == "running")
        )
    ).scalar_one()
    if running >= 2:
        raise ChaosError(
            "at most 2 chaos experiments may run at once per workspace", code="too_many_experiments"
        )
    for spec in plan.faults:
        if spec.node_key and spec.node_key not in {n.key for n in result.spec.nodes}:
            raise ChaosError(f"fault targets unknown node {spec.node_key!r}", code="invalid_plan")
    now = utcnow()
    fresh = now - timedelta(
        seconds=30
    )  # "active" rows can outlive a dead worker; count only recent heartbeats
    workers = (
        await session.execute(
            select(func.count())
            .select_from(Worker)
            .where(Worker.status == "active", Worker.last_heartbeat_at >= fresh)
        )
    ).scalar_one()
    s = get_settings()
    exp = ChaosExperiment(
        workspace_id=workspace_id,
        name=name,
        workflow_definition_id=wf.id,
        plan=plan.model_dump(mode="json"),
        plan_sha256=plan_hash(plan),
        version_overrides={str(k): str(v) for k, v in (version_overrides or {}).items()},
        status="running",
        created_by=user_id,
        started_at=now,
        conditions={
            "workflow": wf.name,
            "workflow_version": wf.version,
            "seed": plan.seed,
            "trials": plan.trials,
            "recovery_policy": plan.recovery_policy,
            "approver": f"simulated:{plan.approver}",
            "active_workers_at_start": workers,
            "execution_mode": s.execution_mode,
            "lease_ttl_seconds": s.lease_ttl_seconds,
            "agent_version_overrides": len(version_overrides or {}),
            "reviewer_note": "Approvals in experiment runs are decided by a simulated reviewer, not a human.",
        },
    )
    session.add(exp)
    await session.flush()
    affected = plan.affected_cases()
    for i in range(plan.trials):
        override: dict[str, Any] = {}
        for idx, spec in enumerate(plan.faults):
            if spec.type != FaultType.BUDGET_EXHAUSTION:
                continue
            r = roll(plan.seed, i, "__run__", spec.type.value, Point.RUN_CREATION.value, 0, f"{idx}:")
            if r < spec.probability and i in affected:
                override = {"max_cost_usd": 0.0}
        run = await core.materialize_run(
            session,
            workflow=wf,
            spec=result.spec,
            run_input=plan.run_input,
            requested_by=user_id,
            agents_by_name=agents,
            version_overrides=version_overrides,
            purpose="chaos",
            chaos_experiment_id=exp.id,
            chaos_case_index=i,
            budget_override=override or None,
        )
        if plan.recovery_policy in RETRY_BY_POLICY:
            await session.execute(
                update(WorkflowStep)
                .where(WorkflowStep.run_id == run.id)
                .values(max_attempts=RETRY_BY_POLICY[plan.recovery_policy])
            )
        if override:
            session.add(
                ChaosInjection(
                    experiment_id=exp.id,
                    run_id=run.id,
                    case_index=i,
                    fault_index=0,
                    fault_type="budget_exhaustion",
                    point="run_creation",
                    step_key=None,
                    attempt_number=None,
                    call_key="",
                    detail={"max_cost_usd": 0.0},
                    injected_at=utcnow(),
                )
            )
    await session.flush()
    return exp


async def abort_experiment(session: AsyncSession, experiment_id: uuid.UUID, reason: str) -> ChaosExperiment:
    exp = (
        await session.execute(
            select(ChaosExperiment).where(ChaosExperiment.id == experiment_id).with_for_update()
        )
    ).scalar_one_or_none()
    if exp is None:
        raise ChaosError("experiment not found", code="not_found")
    if exp.status != "running":
        raise ChaosError(f"experiment already {exp.status}", code="experiment_finished")
    await _finalize(session, exp, status="cancelled", reason=reason)
    return exp


async def _finalize(
    session: AsyncSession, exp: ChaosExperiment, *, status: str, reason: str | None = None
) -> None:
    runs = list(
        (
            await session.execute(select(WorkflowRun).where(WorkflowRun.chaos_experiment_id == exp.id))
        ).scalars()
    )
    for r in runs:
        if r.status not in {s.value for s in TERMINAL_RUN}:
            with contextlib.suppress(core.EngineError):  # already finishing
                await core.cancel_run(session, r.id, user_id=None)
    await session.flush()
    results = await compute_scorecard(session, exp)
    results["partial"] = status != "completed"
    exp.results = results
    exp.status = status
    exp.abort_reason = reason
    exp.completed_at = utcnow()


async def chaos_tick() -> int:
    """Coordinator duty: simulated reviewer, safety limits, window expiry and finalization."""

    async def _ids(session: AsyncSession) -> list[uuid.UUID]:
        return list(
            (
                await session.execute(select(ChaosExperiment.id).where(ChaosExperiment.status == "running"))
            ).scalars()
        )

    ids = await run_in_transaction(_ids)
    done = 0
    for exp_id in ids:

        async def _do(session: AsyncSession, exp_id: uuid.UUID = exp_id) -> bool:
            exp = (
                await session.execute(
                    select(ChaosExperiment)
                    .where(ChaosExperiment.id == exp_id, ChaosExperiment.status == "running")
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if exp is None:
                return False
            plan = ChaosPlan.model_validate(exp.plan)
            runs = list(
                (
                    await session.execute(
                        select(WorkflowRun).where(WorkflowRun.chaos_experiment_id == exp.id)
                    )
                ).scalars()
            )
            # Actual (estimated) spend only. Reservations are transient holds sized for the worst case per call: counting
            # them cancelled healthy experiments whenever many steps were in flight at once (found by the benchmark).
            spent = sum(float(r.cost_spent_usd) for r in runs)
            now = utcnow()
            if spent > plan.safety_budget_usd:
                await _finalize(
                    session,
                    exp,
                    status="cancelled",
                    reason=f"safety budget exceeded (${spent:.4f} > ${plan.safety_budget_usd})",
                )
                return True
            await _simulated_reviewer(session, exp, plan, runs)
            finished = all(r.status in {s.value for s in TERMINAL_RUN} for r in runs)
            if finished:
                await _finalize(session, exp, status="completed")
                return True
            if exp.started_at and now > exp.started_at + timedelta(
                seconds=plan.duration_seconds + GRACE_SECONDS
            ):
                await _finalize(
                    session, exp, status="completed", reason="window elapsed; unfinished runs cancelled"
                )
                return True
            return False

        if await run_in_transaction(_do):
            done += 1
    return done


async def _simulated_reviewer(
    session: AsyncSession, exp: ChaosExperiment, plan: ChaosPlan, runs: list[WorkflowRun]
) -> None:
    if plan.approver != "auto_approve":
        return
    live = {r.id: r for r in runs if r.status not in {s.value for s in TERMINAL_RUN}}
    if not live:
        return
    pending = list(
        (
            await session.execute(
                select(ApprovalRequest).where(
                    ApprovalRequest.run_id.in_(list(live)), ApprovalRequest.status == "pending"
                )
            )
        ).scalars()
    )
    now = utcnow()
    for ap in pending:
        run = live[ap.run_id]
        step_key = (
            await session.execute(select(WorkflowStep.node_key).where(WorkflowStep.id == ap.step_id))
        ).scalar_one_or_none()
        delay = 0.0
        reject = False
        for idx, spec in enumerate(plan.faults):
            if spec.type not in (FaultType.APPROVAL_DELAY, FaultType.APPROVAL_REJECTION):
                continue
            if (run.chaos_case_index or 0) not in plan.affected_cases():
                continue
            if (run.chaos_case_index or 0) not in plan.affected_cases():
                continue
            r = roll(
                plan.seed,
                run.chaos_case_index or 0,
                step_key or "",
                spec.type.value,
                Point.APPROVAL.value,
                0,
                f"{idx}:{ap.requested_action}",
            )
            if r >= spec.probability:
                continue
            fired = await claim_injection_in(
                session,
                experiment_id=exp.id,
                run_id=run.id,
                case_index=run.chaos_case_index or 0,
                fault_index=idx,
                spec=spec,
                step_key=step_key,
                attempt=None,
                key=ap.requested_action,
                detail={"approval_id": str(ap.id)},
            )
            if fired is None and not await _already_fired(
                session, exp.id, run.chaos_case_index or 0, idx, ap.requested_action
            ):
                continue
            if spec.type == FaultType.APPROVAL_DELAY:
                delay = max(delay, float(spec.params.get("delay_seconds", 3)))
            else:
                reject = True
        if (now - ap.requested_at).total_seconds() < delay:
            continue
        await core.resolve_approval(
            session,
            ap.id,
            decision=ApprovalStatus.REJECTED if reject else ApprovalStatus.APPROVED,
            user_id=None,
            reason="chaos: simulated reviewer " + ("rejected" if reject else f"approved after {delay:.0f}s"),
        )


async def _already_fired(session: AsyncSession, exp_id: uuid.UUID, case: int, idx: int, key: str) -> bool:
    return bool(
        (
            await session.execute(
                select(func.count())
                .select_from(ChaosInjection)
                .where(
                    ChaosInjection.experiment_id == exp_id,
                    ChaosInjection.case_index == case,
                    ChaosInjection.fault_index == idx,
                    ChaosInjection.call_key == key,
                )
            )
        ).scalar_one()
    )
