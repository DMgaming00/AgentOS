"""Safe deployment controller: promotion requests, admin approval, canary, rollback, monitoring.

Invariants (enforced here and by DB triggers):
* only an experiment with decision ``eligible`` can be requested for promotion;
* an administrator must approve; requests are single-use and expire;
* if the agent's active version changed after the experiment, the request is stale;
* deployments are append-only records; rollback creates a new record;
* already-created runs keep their pinned versions (pinning happens at run creation).
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.events import emit
from agentos_core.autopilot.policy import load_policy
from agentos_core.autopilot.versions import lock_agent, mirror_to_agent, record_deployment
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDeployment,
    DeploymentRequest,
    OptimizationCandidate,
    OptimizationExperiment,
    RolloutEvent,
    WorkflowRun,
    WorkflowStep,
    Workspace,
)
from agentos_core.engine.tx import run_in_transaction
from agentos_core.util import utcnow


class DeploymentError(Exception):
    def __init__(self, message: str, code: str = "deployment_error", status: int = 409) -> None:
        super().__init__(message)
        self.code, self.status = code, status


async def request_promotion(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    experiment_id: uuid.UUID,
    user_id: uuid.UUID,
    mode: str = "full",
    canary_percent: int = 0,
    reason: str = "",
) -> DeploymentRequest:
    exp = await session.get(OptimizationExperiment, experiment_id)
    if exp is None or exp.workspace_id != workspace_id:
        raise DeploymentError("experiment not found", code="not_found", status=404)
    if exp.status != "completed" or exp.decision != "eligible":
        raise DeploymentError(
            f"experiment decision is {exp.decision or exp.status!r}; only eligible candidates can "
            "be promoted",
            code="not_eligible",
        )
    if mode == "canary" and not 1 <= canary_percent <= 99:
        raise DeploymentError("canary_percent must be 1..99", code="invalid_canary", status=422)
    agent = await lock_agent(session, exp.agent_id)
    if agent.active_version_id != exp.baseline_version_id:
        raise DeploymentError(
            "the agent's active version changed since this experiment; re-run the experiment "
            "against the current version",
            code="stale_experiment",
        )
    pending = (
        await session.execute(
            select(DeploymentRequest).where(
                DeploymentRequest.agent_id == agent.id, DeploymentRequest.status == "pending"
            )
        )
    ).scalar_one_or_none()
    if pending is not None:
        if pending.experiment_id == exp.id and pending.mode == mode:
            return pending  # duplicate request collapses onto the open one
        raise DeploymentError("another promotion request for this agent is pending", code="request_pending")
    ws = await session.get(Workspace, workspace_id)
    policy = load_policy(ws.settings if ws else {})
    now = utcnow()
    req = DeploymentRequest(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        agent_id=agent.id,
        experiment_id=exp.id,
        candidate_version_id=exp.candidate_version_id,
        baseline_version_id=exp.baseline_version_id,
        mode=mode,
        canary_percent=canary_percent if mode == "canary" else 100,
        status="pending",
        reason=reason[:2000],
        requested_by=user_id,
        requested_at=now,
        expires_at=now + timedelta(hours=policy.approval_ttl_hours),
    )
    session.add(req)
    await session.flush()
    await emit(
        session,
        workspace_id,
        "approval.requested",
        {
            "request_id": str(req.id),
            "agent": agent.name,
            "mode": mode,
            "experiment_id": str(exp.id),
            "expires_at": req.expires_at.isoformat(),
        },
    )
    return req


async def decide_request(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    request_id: uuid.UUID,
    approve: bool,
    user_id: uuid.UUID,
    reason: str | None,
) -> DeploymentRequest:
    req = (
        await session.execute(
            select(DeploymentRequest)
            .where(DeploymentRequest.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if req is None or req.workspace_id != workspace_id:
        raise DeploymentError("request not found", code="not_found", status=404)
    if req.status != "pending":
        raise DeploymentError(f"request already {req.status}", code="request_not_pending")
    now = utcnow()
    if req.expires_at <= now:
        # Returned (not raised) so the expiry is committed; callers report it as a conflict.
        req.status, req.decided_at, req.decision_reason = "expired", now, "expired before a decision"
        await emit(
            session, workspace_id, "approval.decided", {"request_id": str(req.id), "status": "expired"}
        )
        return req
    req.decided_by, req.decided_at, req.decision_reason = user_id, now, reason
    if not approve:
        req.status = "rejected"
        await emit(
            session,
            workspace_id,
            "approval.decided",
            {"request_id": str(req.id), "status": "rejected", "reason": reason},
        )
        return req
    agent = await lock_agent(session, req.agent_id)
    if agent.active_version_id != req.baseline_version_id:
        req.status = "rejected"
        req.decision_reason = "stale: the agent's active version changed after the experiment"
        await emit(
            session,
            workspace_id,
            "approval.decided",
            {"request_id": str(req.id), "status": "rejected", "reason": req.decision_reason},
        )
        return req
    exp = await session.get(OptimizationExperiment, req.experiment_id)
    if exp is None or exp.decision != "eligible":
        raise DeploymentError("experiment is not eligible", code="not_eligible")
    version = await session.get(AgentConfigVersion, req.candidate_version_id)
    assert version is not None
    req.status = "approved"
    if req.mode == "canary":
        agent.canary_version_id, agent.canary_percent = version.id, req.canary_percent
        dep = await record_deployment(
            session,
            agent,
            version,
            deployment_type="canary",
            approved_by=user_id,
            reason=req.reason or "canary",
            experiment_id=exp.id,
            request_id=req.id,
            canary_percent=req.canary_percent,
            supersede=False,
        )
        event_type = "canary.started"
    else:
        dep = await record_deployment(
            session,
            agent,
            version,
            deployment_type="promotion",
            approved_by=user_id,
            reason=req.reason or "promotion",
            experiment_id=exp.id,
            request_id=req.id,
        )
        agent.active_version_id = version.id
        agent.canary_version_id, agent.canary_percent = None, 0
        mirror_to_agent(agent, AgentConfig.model_validate(version.config_snapshot))
        event_type = "version.promoted"
    if exp.candidate_id:
        cand = await session.get(OptimizationCandidate, exp.candidate_id)
        if cand is not None:
            cand.status = "promoted"
    session.add(
        RolloutEvent(
            deployment_id=dep.id,
            event_type="promoted" if req.mode == "full" else "canary_started",
            payload={
                "request_id": str(req.id),
                "approved_by": str(user_id),
                "mode": req.mode,
                "canary_percent": req.canary_percent,
                "experiment_id": str(exp.id),
            },
        )
    )
    await emit(session, workspace_id, "approval.decided", {"request_id": str(req.id), "status": "approved"})
    await emit(
        session,
        workspace_id,
        event_type,
        {
            "agent": agent.name,
            "version": version.version,
            "deployment_id": str(dep.id),
            "mode": req.mode,
            "canary_percent": req.canary_percent,
        },
    )
    return req


async def rollback(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    user_id: uuid.UUID,
    reason: str,
    target_version_id: uuid.UUID | None = None,
) -> AgentDeployment:
    agent = await lock_agent(session, agent_id)
    if agent.workspace_id != workspace_id:
        raise DeploymentError("agent not found", code="not_found", status=404)
    if not reason.strip():
        raise DeploymentError("a rollback reason is required", code="reason_required", status=422)
    current = (
        (
            await session.execute(
                select(AgentDeployment)
                .where(
                    AgentDeployment.agent_id == agent_id,
                    AgentDeployment.status == "active",
                    AgentDeployment.deployment_type != "canary",
                )
                .order_by(AgentDeployment.deployed_at.desc())
            )
        )
        .scalars()
        .first()
    )
    if target_version_id is None:
        if current is None or current.previous_version_id is None:
            raise DeploymentError("no previous version to roll back to", code="nothing_to_roll_back")
        target_version_id = current.previous_version_id
    deployed_before = (
        await session.execute(
            select(func.count())
            .select_from(AgentDeployment)
            .where(
                AgentDeployment.agent_id == agent_id, AgentDeployment.config_version_id == target_version_id
            )
        )
    ).scalar_one()
    if not deployed_before:
        raise DeploymentError(
            "rollback target must be a previously deployed version", code="invalid_target", status=422
        )
    if target_version_id == agent.active_version_id and agent.canary_version_id is None:
        raise DeploymentError("target is already the active version", code="noop")
    target = await session.get(AgentConfigVersion, target_version_id)
    assert target is not None
    now = utcnow()
    rolled_from = agent.active_version_id
    for d in (
        await session.execute(
            select(AgentDeployment).where(
                AgentDeployment.agent_id == agent_id, AgentDeployment.status == "active"
            )
        )
    ).scalars():
        d.status, d.rolled_back_at, d.rolled_back_by = "rolled_back", now, user_id
        session.add(
            RolloutEvent(
                deployment_id=d.id,
                event_type="rolled_back",
                payload={"by": str(user_id), "reason": reason, "restored_version_id": str(target.id)},
            )
        )
    dep = await record_deployment(
        session,
        agent,
        target,
        deployment_type="rollback",
        approved_by=user_id,
        reason=reason,
        supersede=False,
    )
    agent.active_version_id = target.id
    agent.canary_version_id, agent.canary_percent = None, 0
    mirror_to_agent(agent, AgentConfig.model_validate(target.config_snapshot))
    await emit(
        session,
        workspace_id,
        "version.rolled_back",
        {
            "agent": agent.name,
            "restored_version": target.version,
            "rolled_back_from_version_id": str(rolled_from),
            "by": str(user_id),
            "reason": reason,
            "deployment_id": str(dep.id),
        },
    )
    return dep


# ----------------------------------------------------------------------------- monitoring


async def live_stats(
    session: AsyncSession, version_id: uuid.UUID, *, since: Any = None, until: Any = None
) -> dict[str, Any]:
    """Outcomes of *live* (non-evaluation) steps pinned to a version."""
    q = (
        select(WorkflowStep.status, func.count())
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
        .where(
            WorkflowStep.agent_version_id == version_id,
            WorkflowRun.purpose == "workflow",
            WorkflowStep.status.in_(["succeeded", "failed"]),
        )
        .group_by(WorkflowStep.status)
    )
    if since is not None:
        q = q.where(WorkflowStep.created_at >= since)
    if until is not None:
        q = q.where(WorkflowStep.created_at < until)
    counts = dict((await session.execute(q)).all())
    n = counts.get("succeeded", 0) + counts.get("failed", 0)
    return {
        "n": n,
        "failed": counts.get("failed", 0),
        "failure_rate": (counts.get("failed", 0) / n) if n else None,
    }


async def deployment_health(session: AsyncSession, dep: AgentDeployment) -> dict[str, Any]:
    ws = await session.get(Workspace, dep.workspace_id)
    mon = load_policy(ws.settings if ws else {}).monitoring
    current = await live_stats(session, dep.config_version_id, since=dep.deployed_at)
    previous = (
        await live_stats(session, dep.previous_version_id, until=dep.deployed_at)
        if dep.previous_version_id
        else None
    )
    regression = bool(
        previous
        and previous["failure_rate"] is not None
        and current["failure_rate"] is not None
        and current["n"] >= mon.min_live_samples
        and current["failure_rate"] - previous["failure_rate"] >= mon.failure_rate_increase_threshold
    )
    return {
        "deployment_id": str(dep.id),
        "current": current,
        "previous": previous,
        "regression": regression,
        "thresholds": mon.model_dump(),
        "note": None
        if current["n"] >= mon.min_live_samples
        else f"only {current['n']} live executions since deployment (need {mon.min_live_samples})",
    }


async def monitoring_tick() -> int:
    """Flag deployments whose live failure rate rose past the threshold (once per deployment)."""
    from agentos_core.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        deps = (
            (
                await s.execute(
                    select(AgentDeployment.id).where(
                        AgentDeployment.status == "active",
                        AgentDeployment.deployment_type.in_(["promotion", "canary", "manual_edit"]),
                    )
                )
            )
            .scalars()
            .all()
        )
    flagged = 0
    for dep_id in deps:

        async def _do(session: AsyncSession, dep_id: uuid.UUID = dep_id) -> int:
            dep = await session.get(AgentDeployment, dep_id)
            if dep is None:
                return 0
            already = (
                await session.execute(
                    select(func.count())
                    .select_from(RolloutEvent)
                    .where(
                        RolloutEvent.deployment_id == dep_id, RolloutEvent.event_type == "regression_detected"
                    )
                )
            ).scalar_one()
            if already:
                return 0
            health = await deployment_health(session, dep)
            if not health["regression"]:
                return 0
            session.add(RolloutEvent(deployment_id=dep_id, event_type="regression_detected", payload=health))
            await emit(
                session,
                dep.workspace_id,
                "monitoring.regression_detected",
                {"deployment_id": str(dep_id), "agent_id": str(dep.agent_id), **health},
            )
            ws = await session.get(Workspace, dep.workspace_id)
            if load_policy(ws.settings if ws else {}).monitoring.auto_rollback and dep.previous_version_id:
                await rollback(
                    session,
                    workspace_id=dep.workspace_id,
                    agent_id=dep.agent_id,
                    user_id=None,  # type: ignore[arg-type]
                    reason="automatic rollback: live failure-rate regression",
                )
            return 1

        flagged += await run_in_transaction(_do)
    return flagged
