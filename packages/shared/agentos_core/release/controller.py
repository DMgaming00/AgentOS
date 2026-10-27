"""Release controller: environments, staged canary rollouts, gated promotion, deterministic rollback.

* ``development`` and ``staging`` are routing pins (a run created with that environment uses the pinned version).
  Development needs no gate. Staging needs the non-human promotion gates.
* ``production`` is the production-style local simulation (the agent's own active/canary pointers). A release there needs
  every non-human gate, then an administrator's approval of the first canary stage (the existing deployment request flow),
  then an administrator's explicit advance through each stage of the schedule (default 5, 10, 25, 50, 100%).
  Reaching 100% promotes the candidate. Each stage change requires its own gates (live evidence, rules clear).
* Rollback is deterministic: configurable rules over measured canary-vs-baseline samples, each with a minimum evidence
  window. With ``auto_rollback`` the coordinator rolls back by itself; otherwise it records a recommendation.
* Runs are pinned at creation: a stage change never alters a run that already started.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.deployment import DeploymentError, request_promotion, rollback
from agentos_core.autopilot.events import emit
from agentos_core.autopilot.versions import lock_agent, mirror_to_agent, record_deployment
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    AgentDeployment,
    AgentEnvironmentPin,
    DeploymentRequest,
    OptimizationCandidate,
    OptimizationExperiment,
    ReleaseEvent,
    ReleasePlan,
)
from agentos_core.db.session import get_sessionmaker
from agentos_core.engine.tx import run_in_transaction, unit_of_work
from agentos_core.release import ALLOWED_PERCENTS, DEFAULT_SCHEDULE, ENVIRONMENTS
from agentos_core.release.gates import blocking, budget_gate, promotion_gates, security_gate
from agentos_core.release.rules import RollbackRules, evaluate
from agentos_core.release.stats import load_arm
from agentos_core.util import utcnow


class ReleaseError(Exception):
    def __init__(
        self, message: str, code: str = "release_error", status: int = 409, details: Any = None
    ) -> None:
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def validate_schedule(schedule: list[int] | None) -> list[int]:
    s = list(schedule or DEFAULT_SCHEDULE)
    if len(s) < 2 or s != sorted(set(s)) or s[-1] != 100 or any(p not in ALLOWED_PERCENTS for p in s):
        raise ReleaseError(
            f"schedule must be an increasing list of {list(ALLOWED_PERCENTS)} ending at 100 with at least one canary stage",
            "invalid_schedule",
            422,
        )
    return s


def add_event(
    session: AsyncSession, plan: ReleasePlan, event_type: str, actor: str | None, **payload: Any
) -> None:
    session.add(ReleaseEvent(plan_id=plan.id, event_type=event_type, actor=actor, payload=payload))


async def _independent_event(plan_id: uuid.UUID, event_type: str, actor: str | None, **payload: Any) -> None:
    """Record an event in its own transaction (for refusals: the caller is about to raise and roll back)."""
    async with unit_of_work() as s:
        s.add(ReleaseEvent(plan_id=plan_id, event_type=event_type, actor=actor, payload=payload))


async def _version(
    session: AsyncSession, agent: AgentDefinition, version_id: uuid.UUID
) -> AgentConfigVersion:
    v = await session.get(AgentConfigVersion, version_id)
    if v is None or v.agent_id != agent.id:
        raise ReleaseError("version does not belong to this agent", "invalid_version", 422)
    return v


async def pin_environment(
    session: AsyncSession,
    agent: AgentDefinition,
    environment: str,
    version_id: uuid.UUID,
    user_id: uuid.UUID | None,
) -> None:
    await session.execute(
        insert(AgentEnvironmentPin)
        .values(
            agent_id=agent.id,
            environment=environment,
            version_id=version_id,
            updated_by=user_id,
            updated_at=utcnow(),
        )
        .on_conflict_do_update(
            index_elements=["agent_id", "environment"],
            set_={"version_id": version_id, "updated_by": user_id, "updated_at": utcnow()},
        )
    )


async def create_release(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    environment: str,
    experiment_id: uuid.UUID | None = None,
    candidate_version_id: uuid.UUID | None = None,
    schedule: list[int] | None = None,
    rules: dict[str, Any] | None = None,
    auto_rollback: bool = False,
    min_stage_samples: int = 10,
    min_stage_seconds: int = 0,
    seed: str | None = None,
    reason: str = "",
) -> ReleasePlan:
    if environment not in ENVIRONMENTS:
        raise ReleaseError(
            f"environment must be one of {', '.join(ENVIRONMENTS)}", "invalid_environment", 422
        )
    agent = await lock_agent(session, agent_id)
    if agent.workspace_id != workspace_id:
        raise ReleaseError("agent not found", "not_found", 404)
    exp = await session.get(OptimizationExperiment, experiment_id) if experiment_id else None
    if experiment_id and (exp is None or exp.workspace_id != workspace_id or exp.agent_id != agent.id):
        raise ReleaseError("experiment not found for this agent", "not_found", 404)
    version_id = candidate_version_id or (exp.candidate_version_id if exp else None)
    if version_id is None:
        raise ReleaseError("choose an experiment or a candidate version", "candidate_required", 422)
    version = await _version(session, agent, version_id)
    baseline_id = agent.active_version_id
    assert baseline_id is not None
    rules_cfg = RollbackRules.model_validate(rules or {})
    sched = validate_schedule(schedule) if environment == "production" else [100]
    plan = ReleasePlan(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        agent_id=agent.id,
        environment=environment,
        candidate_version_id=version.id,
        baseline_version_id=baseline_id,
        experiment_id=exp.id if exp else None,
        status="pending_approval",
        schedule=sched,
        stage_index=0,
        seed=seed or secrets.token_hex(8),
        rules=rules_cfg.model_dump(),
        auto_rollback=auto_rollback,
        min_stage_samples=min_stage_samples,
        min_stage_seconds=min_stage_seconds,
        created_by=user_id,
    )
    live = (
        await session.execute(
            select(ReleasePlan.id).where(
                ReleasePlan.agent_id == agent.id,
                ReleasePlan.environment == environment,
                ReleasePlan.status.in_(["pending_approval", "active"]),
            )
        )
    ).first()
    if live is not None:
        raise ReleaseError(
            f"a release for this agent in {environment} is already in progress", "release_in_progress"
        )
    actor = str(user_id)
    if environment == "development":
        plan.status, plan.completed_at, plan.end_reason = (
            "completed",
            utcnow(),
            "pinned to development (no gates required)",
        )
        session.add(plan)
        await session.flush()
        await pin_environment(session, agent, "development", version.id, user_id)
        add_event(
            session,
            plan,
            "environment_pinned",
            actor,
            environment="development",
            version=version.version,
            reason=reason,
        )
        return plan
    gates = await promotion_gates(
        session,
        workspace_id=workspace_id,
        agent=agent,
        experiment=exp,
        require_approval=environment == "production",
    )
    fail = blocking(gates)
    if fail:
        raise ReleaseError(
            "promotion gates failed: " + "; ".join(g["gate"] for g in fail),
            "gates_failed",
            409,
            {"gates": gates},
        )
    session.add(plan)
    await session.flush()
    add_event(session, plan, "gates_evaluated", actor, gates=gates)
    if environment == "staging":
        plan.status, plan.completed_at, plan.end_reason = (
            "completed",
            utcnow(),
            "pinned to staging after gates passed",
        )
        await pin_environment(session, agent, "staging", version.id, user_id)
        add_event(
            session,
            plan,
            "environment_pinned",
            actor,
            environment="staging",
            version=version.version,
            reason=reason,
        )
        return plan
    assert (
        exp is not None
    )  # production gates require an experiment, so the gate list above already failed without one
    try:
        req = await request_promotion(
            session,
            workspace_id=workspace_id,
            experiment_id=exp.id,
            user_id=user_id,
            mode="canary",
            canary_percent=sched[0],
            reason=reason,
        )
    except DeploymentError as exc:
        raise ReleaseError(str(exc), exc.code, exc.status) from None
    plan.request_id = req.id
    add_event(
        session,
        plan,
        "release_requested",
        actor,
        schedule=sched,
        request_id=str(req.id),
        auto_rollback=auto_rollback,
        expires_at=req.expires_at.isoformat(),
    )
    await emit(
        session,
        workspace_id,
        "release.requested",
        {"plan_id": str(plan.id), "agent": agent.name, "schedule": sched},
    )
    return plan


async def on_request_decided(session: AsyncSession, req: DeploymentRequest) -> None:
    """Called by the deployment controller after it decides a request: activates or aborts the linked release plan."""
    plan = (
        await session.execute(
            select(ReleasePlan).where(ReleasePlan.request_id == req.id).with_for_update(key_share=True)
        )
    ).scalar_one_or_none()
    if plan is None or plan.status != "pending_approval":
        return
    now = utcnow()
    if req.status == "approved":
        agent = await lock_agent(session, plan.agent_id)
        agent.canary_seed = plan.seed
        plan.status, plan.stage_started_at = "active", now
        add_event(
            session,
            plan,
            "stage_started",
            str(req.decided_by),
            stage_index=0,
            percent=plan.schedule[0],
            approved_by=str(req.decided_by),
            seed=plan.seed,
        )
    else:
        plan.status, plan.completed_at, plan.end_reason = (
            "aborted",
            now,
            f"deployment request {req.status}: {req.decision_reason or ''}".strip(),
        )
        add_event(
            session, plan, "aborted", str(req.decided_by) if req.decided_by else None, reason=plan.end_reason
        )


# ----------------------------------------------------------------------------- evaluation


async def evaluate_plan(
    session: AsyncSession, plan: ReleasePlan, *, now: datetime | None = None
) -> dict[str, Any]:
    now = now or utcnow()
    since = plan.stage_started_at or plan.created_at
    canary = await load_arm(session, plan.candidate_version_id, environment=plan.environment, since=since)
    base = await load_arm(session, plan.baseline_version_id, environment=plan.environment, since=since)
    whole = await load_arm(
        session, plan.candidate_version_id, environment=plan.environment, since=plan.created_at
    )
    canary.denied_tool_calls = whole.denied_tool_calls  # a policy violation in any earlier stage still counts
    results = evaluate(RollbackRules.model_validate(plan.rules), canary, base, now=now)
    return {
        "stage_index": plan.stage_index,
        "percent": plan.schedule[plan.stage_index],
        "since": since.isoformat(),
        "canary_steps": len(canary.samples),
        "baseline_steps": len(base.samples),
        "rules": results,
        "triggered": [r["rule"] for r in results if r["status"] == "triggered"],
    }


async def stage_gates(
    session: AsyncSession, plan: ReleasePlan, evaluation: dict[str, Any]
) -> list[dict[str, Any]]:
    from agentos_core.release.gates import gate

    now = utcnow()
    elapsed = (now - (plan.stage_started_at or now)).total_seconds()
    exp = await session.get(OptimizationExperiment, plan.experiment_id) if plan.experiment_id else None
    return [
        gate(
            "stage_samples",
            evaluation["canary_steps"] >= plan.min_stage_samples,
            f"{evaluation['canary_steps']} live canary steps in this stage (minimum {plan.min_stage_samples})",
            n=evaluation["canary_steps"],
        ),
        gate(
            "stage_duration",
            elapsed >= plan.min_stage_seconds,
            f"{int(elapsed)}s in this stage (minimum {plan.min_stage_seconds}s)",
            seconds=int(elapsed),
        ),
        gate(
            "rollback_rules_clear",
            not evaluation["triggered"],
            "no rollback rule is triggered"
            if not evaluation["triggered"]
            else f"triggered: {', '.join(evaluation['triggered'])}",
            triggered=evaluation["triggered"],
        ),
        await budget_gate(session, plan.workspace_id, plan.agent_id, exp),
        await security_gate(session, plan.workspace_id),
    ]


async def advance(session: AsyncSession, plan_id: uuid.UUID, *, user_id: uuid.UUID) -> ReleasePlan:
    plan = (
        await session.execute(
            select(ReleasePlan)
            .where(ReleasePlan.id == plan_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise ReleaseError("release not found", "not_found", 404)
    if plan.status != "active" or plan.environment != "production":
        raise ReleaseError(
            f"release is {plan.status}; only an active production release can advance", "not_active"
        )
    evaluation = await evaluate_plan(session, plan)
    gates = await stage_gates(session, plan, evaluation)
    fail = blocking(gates)
    if fail:
        await _independent_event(
            plan.id, "advance_blocked", str(user_id), stage_index=plan.stage_index, gates=gates
        )
        raise ReleaseError(
            "stage gates failed: " + "; ".join(g["gate"] for g in fail), "gates_failed", 409, {"gates": gates}
        )
    agent = await lock_agent(session, plan.agent_id)
    version = await session.get(AgentConfigVersion, plan.candidate_version_id)
    assert version is not None
    nxt = plan.stage_index + 1
    percent = plan.schedule[nxt]
    now = utcnow()
    if percent == 100:
        for d in (
            await session.execute(
                select(AgentDeployment).where(
                    AgentDeployment.agent_id == agent.id,
                    AgentDeployment.status == "active",
                    AgentDeployment.deployment_type == "canary",
                )
            )
        ).scalars():
            d.status = "superseded"
        dep = await record_deployment(
            session,
            agent,
            version,
            deployment_type="promotion",
            approved_by=user_id,
            reason=f"release {plan.id}: promoted after staged rollout {plan.schedule}",
            experiment_id=plan.experiment_id,
            request_id=plan.request_id,
        )
        agent.active_version_id = version.id
        agent.canary_version_id, agent.canary_percent, agent.canary_seed = None, 0, None
        mirror_to_agent(agent, AgentConfig.model_validate(version.config_snapshot))
        exp = await session.get(OptimizationExperiment, plan.experiment_id) if plan.experiment_id else None
        if exp is not None and exp.candidate_id:
            cand = await session.get(OptimizationCandidate, exp.candidate_id)
            if cand is not None:
                cand.status = "promoted"
        plan.stage_index, plan.status, plan.completed_at, plan.end_reason = (
            nxt,
            "completed",
            now,
            "promoted to 100%",
        )
        add_event(session, plan, "completed", str(user_id), deployment_id=str(dep.id), gates=gates)
        await emit(
            session,
            plan.workspace_id,
            "release.completed",
            {"plan_id": str(plan.id), "agent": agent.name, "version": version.version},
        )
        return plan
    for d in (
        await session.execute(
            select(AgentDeployment).where(
                AgentDeployment.agent_id == agent.id,
                AgentDeployment.status == "active",
                AgentDeployment.deployment_type == "canary",
            )
        )
    ).scalars():
        d.status = "superseded"
    dep = await record_deployment(
        session,
        agent,
        version,
        deployment_type="canary",
        approved_by=user_id,
        reason=f"release {plan.id}: stage {nxt} ({percent}%)",
        experiment_id=plan.experiment_id,
        request_id=plan.request_id,
        canary_percent=percent,
        supersede=False,
    )
    agent.canary_percent = percent
    plan.stage_index, plan.stage_started_at = nxt, now
    add_event(
        session,
        plan,
        "stage_advanced",
        str(user_id),
        stage_index=nxt,
        percent=percent,
        deployment_id=str(dep.id),
        gates=gates,
        evaluation=evaluation,
    )
    await emit(
        session,
        plan.workspace_id,
        "release.stage_advanced",
        {"plan_id": str(plan.id), "agent": agent.name, "percent": percent},
    )
    return plan


async def rollback_release(
    session: AsyncSession,
    plan_id: uuid.UUID,
    *,
    user_id: uuid.UUID | None,
    reason: str,
    automatic: bool = False,
    evidence: dict[str, Any] | None = None,
) -> ReleasePlan:
    plan = (
        await session.execute(
            select(ReleasePlan)
            .where(ReleasePlan.id == plan_id)
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if plan is None:
        raise ReleaseError("release not found", "not_found", 404)
    if plan.status != "active":
        raise ReleaseError(f"release is {plan.status}; nothing to roll back", "not_active")
    try:
        dep = await rollback(
            session,
            workspace_id=plan.workspace_id,
            agent_id=plan.agent_id,
            user_id=user_id,
            reason=reason,
            target_version_id=plan.baseline_version_id,
        )
    except DeploymentError as exc:
        raise ReleaseError(str(exc), exc.code, exc.status) from None
    agent = await session.get(AgentDefinition, plan.agent_id)
    if agent is not None:
        agent.canary_seed = None
    plan.status, plan.completed_at, plan.end_reason = "rolled_back", utcnow(), reason
    await emit(
        session,
        plan.workspace_id,
        "release.rolled_back",
        {"plan_id": str(plan.id), "automatic": automatic, "reason": reason},
    )
    add_event(
        session,
        plan,
        "rolled_back",
        str(user_id) if user_id else "system",
        automatic=automatic,
        reason=reason,
        deployment_id=str(dep.id),
        evidence=evidence or {},
    )
    return plan


async def release_tick() -> int:
    """Coordinator duty: evaluate rollback rules for active production releases; roll back (if enabled) or recommend."""
    async with get_sessionmaker()() as s:
        ids = list(
            (
                await s.execute(
                    select(ReleasePlan.id).where(
                        ReleasePlan.status == "active", ReleasePlan.environment == "production"
                    )
                )
            ).scalars()
        )
    acted = 0
    for plan_id in ids:

        async def _do(session: AsyncSession, plan_id: uuid.UUID = plan_id) -> int:
            plan = (
                await session.execute(
                    select(ReleasePlan)
                    .where(ReleasePlan.id == plan_id)
                    .with_for_update(skip_locked=True, key_share=True)
                    .execution_options(populate_existing=True)
                )
            ).scalar_one_or_none()
            if plan is None or plan.status != "active":
                return 0
            ev = await evaluate_plan(session, plan)
            if not ev["triggered"]:
                return 0
            seen = {
                (e.payload.get("stage_index"), e.payload.get("rule"))
                for e in (
                    await session.execute(
                        select(ReleaseEvent).where(
                            ReleaseEvent.plan_id == plan.id, ReleaseEvent.event_type == "rule_triggered"
                        )
                    )
                ).scalars()
            }
            new = 0
            for r in ev["rules"]:
                if r["status"] == "triggered" and (plan.stage_index, r["rule"]) not in seen:
                    add_event(
                        session,
                        plan,
                        "rule_triggered",
                        "system",
                        stage_index=plan.stage_index,
                        rule=r["rule"],
                        reason=r["reason"],
                        measured=r["measured"],
                    )
                    new += 1
            if plan.auto_rollback:
                first = next(r for r in ev["rules"] if r["status"] == "triggered")
                await rollback_release(
                    session,
                    plan.id,
                    user_id=None,
                    reason=f"automatic rollback: {first['rule']}: {first['reason']}",
                    automatic=True,
                    evidence=ev,
                )
                return new + 1
            if new:
                add_event(session, plan, "rollback_recommended", "system", rules=ev["triggered"])
            return new

        acted += await run_in_transaction(_do)
    return acted
