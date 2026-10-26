"""Optimizer Agent and candidate policy validation.

An LLM proposes changes; ``validate_candidate`` decides, in code, whether each change is
allowed. Accepted candidates become immutable agent versions (source='optimizer'); rejected
ones are stored with their violations. Nothing here deploys anything.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.config import AgentConfig, config_diff
from agentos_core.autopilot.events import emit
from agentos_core.autopilot.llm import autopilot_call
from agentos_core.autopilot.policy import (
    NEVER_OPTIMIZABLE,
    NO_SAMPLING_PARAMS,
    OPTIMIZABLE_FIELDS,
    AutopilotPolicy,
    load_policy,
)
from agentos_core.autopilot.versions import ensure_version, lock_agent
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    EvaluationCase,
    EvaluationCaseResult,
    EvaluationDataset,
    EvaluationRun,
    FailureDiagnosis,
    OptimizationCandidate,
    Workspace,
)
from agentos_core.planner import extract_json
from agentos_core.redaction import redact
from agentos_core.tools.registry import get_registry

SYSTEM = """You are the AgentOS Optimizer Agent. Propose candidate agent configuration changes that address a
diagnosed failure. You may only change these fields: {fields}. You may remove tools but never add tools. Use only
the allowed models listed. Budget ceilings, timeouts, approvals, permissions and evaluation data cannot be changed.
For each candidate give the exact new values, a reason per change, expected benefits and risks.
Reply with only JSON: {{"candidates": [{{"title": str, "changes": [{{"field": str, "value": any, "reason": str}}],
"expected_benefits": [str], "risks": [str]}}]}}"""

SAFETY_WEAKENING = re.compile(
    r"\b(ignore|disable|bypass|override|skip|remove)\b.{0,60}\b(platform rules|safety|approval|approvals|permission|"
    r"permissions|restriction|restrictions|guardrails?|policy|policies)\b",
    re.IGNORECASE | re.DOTALL,
)
TEXT_FIELDS = ("system_instructions", "tool_strategy")


class ProposedChange(BaseModel):
    field: str = Field(max_length=100)
    value: Any = None
    reason: str = Field(default="", max_length=2000)


class ProposedCandidate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    changes: list[ProposedChange] = Field(min_length=1, max_length=10)
    expected_benefits: list[str] = Field(default_factory=list, max_length=10)
    risks: list[str] = Field(default_factory=list, max_length=10)


class OptimizerOut(BaseModel):
    candidates: list[ProposedCandidate] = Field(min_length=1, max_length=10)


def _hidden_fingerprints(
    cases: list[tuple[str, dict[str, Any], dict[str, Any]]],
) -> list[tuple[str, str, list[str]]]:
    """(case_key, subject, expected value strings) for hidden-split cases, for leakage checks."""
    out = []
    for key, inp, expected in cases:
        subject = str(inp.get("subject") or "")
        values: list[str] = []
        for v in expected.values():
            if isinstance(v, int | float) and not isinstance(v, bool):
                values += {f"{v}", f"{v:g}", f"{v:,}"}
            elif isinstance(v, str) and len(v) > 3:
                values.append(v)
        out.append((key, subject, values))
    return out


def detect_leakage(text: str, fingerprints: list[tuple[str, str, list[str]]]) -> list[str]:
    hits = []
    low = text.lower()
    for key, subject, values in fingerprints:
        if key.lower() in low:
            hits.append(f"mentions hidden case id {key}")
            continue
        if subject and subject.lower() in low:
            for v in values:
                for m in re.finditer(re.escape(subject.lower()), low):
                    window = low[max(0, m.start() - 120) : m.end() + 120]
                    if re.search(rf"(?<![\d.]){re.escape(v.lower())}(?![\d])", window):
                        hits.append(f"embeds a hidden expected value for {subject} ({key})")
                        break
    return sorted(set(hits))


def validate_candidate(
    baseline: AgentConfig,
    changes: list[ProposedChange],
    policy: AutopilotPolicy,
    *,
    hidden: list[tuple[str, str, list[str]]] | None = None,
) -> tuple[AgentConfig | None, list[str]]:
    """Apply changes under policy. Returns (new_config, violations); new_config is None if any violation."""
    violations: list[str] = []
    data = baseline.model_dump()
    allowed = set(policy.optimizable_fields) & set(OPTIMIZABLE_FIELDS)
    for ch in changes:
        f = ch.field
        if f in NEVER_OPTIMIZABLE or f.split(".")[0] in NEVER_OPTIMIZABLE:
            violations.append(f"{f}: may never be changed by a candidate")
            continue
        if f not in allowed:
            violations.append(f"{f}: not an optimizable field under the workspace policy")
            continue
        if f.startswith("model_configuration."):
            sub = f.split(".", 1)[1]
            if sub == "scenario":
                violations.append(f"{f}: may never be changed")
                continue
            data["model_configuration"] = {**data["model_configuration"], sub: ch.value}
        else:
            data[f] = ch.value
    if violations:
        return None, violations

    # tools: may only shrink
    new_tools = set(data.get("allowed_tool_ids") or [])
    added = new_tools - set(baseline.allowed_tool_ids)
    if added:
        violations.append(
            f"allowed_tool_ids: adds tools not in the agent's approved allowlist: {sorted(added)}"
        )
    unknown = new_tools - set(get_registry().names())
    if unknown:
        violations.append(f"allowed_tool_ids: unknown tools {sorted(unknown)}")
    if set(data.get("tool_descriptions") or {}) - new_tools:
        violations.append("tool_descriptions: may only describe tools the candidate is allowed to use")
    # models
    if (data["provider"], data["model"]) != (baseline.provider, baseline.model) and not policy.allows_model(
        data["provider"], data["model"]
    ):
        violations.append(f"model: {data['provider']}/{data['model']} is not in the policy's approved models")
    mc = data.get("model_configuration") or {}
    if data["provider"] in NO_SAMPLING_PARAMS and any(k in mc for k in ("temperature", "top_p", "top_k")):
        violations.append(
            f"model_configuration: {data['provider']} models used here reject sampling parameters"
        )
    if "max_tokens" in mc and not (isinstance(mc["max_tokens"], int) and 16 <= mc["max_tokens"] <= 128_000):
        violations.append("model_configuration.max_tokens: must be an integer between 16 and 128000")
    # limits
    if not isinstance(data.get("max_steps"), int) or not 1 <= data["max_steps"] <= policy.max_steps_limit:
        violations.append(f"max_steps: must be 1..{policy.max_steps_limit}")
    rm = data.get("retry_max_attempts")
    if rm is not None and (not isinstance(rm, int) or not 1 <= rm <= 5):
        violations.append("retry_max_attempts: must be 1..5")
    # text safety and leakage
    for f in TEXT_FIELDS:
        text = str(data.get(f) or "")
        if text == str(getattr(baseline, f)):
            continue
        if SAFETY_WEAKENING.search(text):
            violations.append(f"{f}: attempts to weaken safety, approval or permission rules")
        for hit in detect_leakage(text, hidden or []):
            violations.append(f"{f}: {hit}")
    for name, desc in (data.get("tool_descriptions") or {}).items():
        if SAFETY_WEAKENING.search(str(desc)):
            violations.append(f"tool_descriptions.{name}: attempts to weaken safety rules")
    if violations:
        return None, violations
    try:
        cfg = AgentConfig.model_validate(data)
    except ValidationError as exc:
        return None, [f"invalid configuration: {e['loc']}: {e['msg']}" for e in exc.errors(include_url=False)]
    if cfg.config_hash() == baseline.config_hash():
        return None, ["candidate is identical to the baseline"]
    return cfg, []


async def _hidden_cases(session: AsyncSession, workspace_id: uuid.UUID) -> list[tuple[str, str, list[str]]]:
    rows = (
        await session.execute(
            select(EvaluationCase.case_key, EvaluationCase.input, EvaluationCase.expected_output)
            .join(EvaluationDataset, EvaluationDataset.id == EvaluationCase.dataset_id)
            .where(EvaluationDataset.workspace_id == workspace_id, EvaluationDataset.visibility == "hidden")
        )
    ).all()
    return _hidden_fingerprints([(k, i, e) for k, i, e in rows])


async def optimizer_context(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    baseline: AgentConfigVersion,
    diagnosis: FailureDiagnosis | None,
    policy: AutopilotPolicy,
) -> dict[str, Any]:
    """What the optimizer may see: baseline config, diagnosis, development-split examples only."""
    dev_rows = (
        await session.execute(
            select(EvaluationCase, EvaluationCaseResult)
            .join(EvaluationDataset, EvaluationDataset.id == EvaluationCase.dataset_id)
            .outerjoin(EvaluationCaseResult, EvaluationCaseResult.case_id == EvaluationCase.id)
            .outerjoin(EvaluationRun, EvaluationRun.id == EvaluationCaseResult.evaluation_run_id)
            .where(
                EvaluationDataset.workspace_id == workspace_id,
                EvaluationDataset.split == "development",
                (EvaluationRun.agent_id == agent_id) | (EvaluationRun.id.is_(None)),
            )
            .order_by(EvaluationCase.case_key)
            .limit(30)
        )
    ).all()
    examples, seen = [], set()
    for case, res in dev_rows:
        if case.case_key in seen:
            continue
        seen.add(case.case_key)
        examples.append(
            {
                "case": case.case_key,
                "task": case.task,
                "input": case.input,
                "expected_output": case.expected_output,  # development split only
                "observed_output": res.observed_output if res else None,
                "passed": res.passed if res else None,
                "failing_checks": [
                    m for m, v in ((res.scores or {}) if res else {}).items() if not v.get("passed")
                ],
            }
        )
        if len(examples) >= 6:
            break
    val_metrics = (
        await session.execute(
            select(EvaluationRun.metrics)
            .join(EvaluationDataset, EvaluationDataset.id == EvaluationRun.dataset_id)
            .where(
                EvaluationRun.agent_version_id == baseline.id,
                EvaluationRun.status == "completed",
                EvaluationDataset.split == "validation",
            )
            .order_by(EvaluationRun.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return {
        "baseline_config": baseline.config_snapshot,
        "baseline_version": baseline.version,
        "diagnosis": (
            {
                "failure_category": diagnosis.failure_category,
                "hypothesis": diagnosis.hypothesis,
                "proposed_remedy": diagnosis.proposed_remedy,
                "confidence": diagnosis.confidence,
            }
            if diagnosis
            else None
        ),
        "development_examples": redact(examples),
        # aggregate numbers only: no hidden case contents or answers
        "validation_metrics": {
            k: (val_metrics or {}).get(k)
            for k in (
                "task_success_rate",
                "schema_compliance",
                "grounding_pass_rate",
                "cost_per_successful_task_usd",
            )
        }
        if val_metrics
        else None,
        "optimizable_fields": policy.optimizable_fields,
        "allowed_models": [m.model_dump() for m in policy.allowed_models],
        "allowed_tools": baseline.config_snapshot.get("allowed_tool_ids", []),
        "max_candidates": policy.max_candidates,
    }


async def _store_candidate(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    baseline: AgentConfigVersion,
    proposal: ProposedCandidate,
    policy: AutopilotPolicy,
    hidden: list[tuple[str, str, list[str]]],
    diagnosis_id: uuid.UUID | None,
    generator: str,
    meta: dict[str, Any],
    user_id: uuid.UUID | None,
) -> OptimizationCandidate:
    base_cfg = AgentConfig.model_validate(baseline.config_snapshot)
    cfg, violations = validate_candidate(base_cfg, proposal.changes, policy, hidden=hidden)
    version = None
    if cfg is not None:
        agent = await lock_agent(session, agent_id)
        version, _ = await ensure_version(
            session,
            agent,
            cfg,
            source="optimizer" if generator == "optimizer" else "manual_candidate",
            parent_version_id=baseline.id,
            created_by=user_id,
            summary=proposal.title,
        )
    cand = OptimizationCandidate(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        agent_id=agent_id,
        diagnosis_id=diagnosis_id,
        baseline_version_id=baseline.id,
        candidate_version_id=version.id if version else None,
        title=proposal.title,
        changes=[c.model_dump(mode="json") for c in proposal.changes],
        diff=config_diff(baseline.config_snapshot, cfg.snapshot()) if cfg else [],
        reason="; ".join(c.reason for c in proposal.changes if c.reason)[:4000],
        expected_benefits=proposal.expected_benefits,
        risk_assessment=proposal.risks,
        policy_violations=violations,
        status="proposed" if cfg else "rejected_policy",
        generator=generator,
        provider=meta.get("provider"),
        model=meta.get("model"),
        simulated=bool(meta.get("simulated")),
        created_by=user_id,
    )
    session.add(cand)
    await session.flush()
    await emit(
        session,
        workspace_id,
        "candidate.generated" if cfg else "candidate.rejected",
        {
            "candidate_id": str(cand.id),
            "title": cand.title,
            "version": version.version if version else None,
            "violations": violations,
            "generator": generator,
        },
    )
    return cand


async def generate_candidates(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    diagnosis_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    count: int | None = None,
    provider: str | None = None,
) -> list[OptimizationCandidate]:
    ws = await session.get(Workspace, workspace_id)
    assert ws is not None
    policy = load_policy(ws.settings)
    n = min(count or policy.max_candidates, policy.max_candidates, 5)
    # No row lock here: holding FOR UPDATE across the model call would block the usage insert
    # (FK KEY SHARE) made in a separate transaction. Versions are created under lock in _store_candidate.
    agent = await session.get(AgentDefinition, agent_id)
    if agent is None or agent.workspace_id != workspace_id or agent.active_version_id is None:
        raise ValueError("agent not found or not versioned")
    baseline = await session.get(AgentConfigVersion, agent.active_version_id)
    assert baseline is not None
    diagnosis = await session.get(FailureDiagnosis, diagnosis_id) if diagnosis_id else None
    if diagnosis is not None and (diagnosis.agent_id != agent_id or diagnosis.status != "hypothesis"):
        raise ValueError("diagnosis must be a valid hypothesis for this agent")
    context = await optimizer_context(session, workspace_id, agent_id, baseline, diagnosis, policy)
    context["max_candidates"] = n
    resp, meta = await autopilot_call(
        provider=provider or policy.optimizer_provider,
        scenario="optimizer",
        system=SYSTEM.format(fields=", ".join(policy.optimizable_fields)),
        user_text=f"<task_input>{json.dumps(context, default=str)}</task_input>",
        workspace_id=workspace_id,
        agent_id=agent_id,
        purpose="optimizer",
        cycle_id=diagnosis_id or uuid.uuid4(),
        max_cycle_spend_usd=policy.max_cycle_spend_usd,
    )
    try:
        proposals = OptimizerOut.model_validate(extract_json(resp.text)).candidates[:n]
    except (ValueError, ValidationError) as exc:
        raise ValueError(f"optimizer returned invalid JSON: {str(exc)[:300]}") from exc
    hidden = await _hidden_cases(session, workspace_id)
    return [
        await _store_candidate(
            session,
            workspace_id=workspace_id,
            agent_id=agent_id,
            baseline=baseline,
            proposal=p,
            policy=policy,
            hidden=hidden,
            diagnosis_id=diagnosis_id,
            generator="optimizer",
            meta=meta,
            user_id=user_id,
        )
        for p in proposals
    ]


async def propose_manual_candidate(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    proposal: ProposedCandidate,
    user_id: uuid.UUID | None,
) -> OptimizationCandidate:
    """A human-authored candidate goes through exactly the same policy checks."""
    ws = await session.get(Workspace, workspace_id)
    assert ws is not None
    agent = await lock_agent(session, agent_id)
    if agent.workspace_id != workspace_id or agent.active_version_id is None:
        raise ValueError("agent not found or not versioned")
    baseline = await session.get(AgentConfigVersion, agent.active_version_id)
    assert baseline is not None
    return await _store_candidate(
        session,
        workspace_id=workspace_id,
        agent_id=agent_id,
        baseline=baseline,
        proposal=proposal,
        policy=load_policy(ws.settings),
        hidden=await _hidden_cases(session, workspace_id),
        diagnosis_id=None,
        generator="manual",
        meta={},
        user_id=user_id,
    )
