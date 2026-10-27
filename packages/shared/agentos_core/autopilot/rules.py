"""Deterministic, versioned candidate-generation rules driven by trace evidence.

Each rule is a pure function of a ``TraceEvidence`` snapshot (what actually happened to this agent: step outcomes, tool
executions, retries) and the baseline configuration. It either proposes a concrete, policy-checkable change with links to
the evidence that triggered it, or proposes nothing. There is no randomness and no model call: the same evidence and
configuration always give the same candidates, and a rule whose symptom is absent stays silent.

Rules only *propose*. Every proposal then goes through ``validate_candidate`` (policy, tool allowlist, leakage and
safety checks) and, if it survives, must still win a paired experiment on held-out cases before it can be eligible. A
rule being triggered is a hypothesis about the cause, never evidence that the change helps.

Candidate types implemented: prompt revisions, tool-description revisions, tool-use strategy (duplicate-call and
empty-retrieval handling), approved tool selection (removal only), model choice among policy-approved models, retry
policy and step limit. Not implemented: tool routing, memory policies and workflow topology proposals (see limitations).
"""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.optimizer import (
    ProposedCandidate,
    ProposedChange,
    _hidden_cases,
    _store_candidate,
)
from agentos_core.autopilot.policy import AutopilotPolicy, load_policy
from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    FailureDiagnosis,
    OptimizationCandidate,
    ToolExecution,
    WorkflowRun,
    WorkflowStep,
    Workspace,
)
from agentos_core.pricing import PricingTable, get_pricing
from agentos_core.tools.registry import get_registry
from agentos_core.util import utcnow

RULESET_VERSION = "rules-v1"
EXCLUDED_PURPOSES = ("chaos", "security", "replay")

JSON_NUMBERS = "Return every numeric value as a plain JSON number with no units or currency symbols."
JSON_NUMBERS_PRESENT = re.compile(
    r"json numbers?|plain (json )?numbers?|numeric json|no currency symbols", re.I
)
NO_REPEAT = "Never repeat a tool call with identical arguments; reuse the earlier result."
NO_REPEAT_PRESENT = re.compile(r"never repeat|do not repeat|reuse the earlier result", re.I)
EMPTY_SEARCH = (
    "If a search returns no results, retry once with only the subject name (no qualifiers); if it is still empty, "
    "report the value as null instead of searching again."
)
EMPTY_SEARCH_PRESENT = re.compile(r"returns no results|retry once with only", re.I)


@dataclass(frozen=True)
class StepFact:
    id: uuid.UUID
    run_id: uuid.UUID
    status: str
    error_type: str | None
    error_message: str
    attempts: int
    failures: int


@dataclass(frozen=True)
class ToolFact:
    id: uuid.UUID
    run_id: uuid.UUID
    step_id: uuid.UUID
    tool: str
    status: str
    error_type: str | None
    error_message: str
    args_hash: str
    result_count: int | None


@dataclass
class TraceEvidence:
    agent_id: uuid.UUID | None
    since: datetime | None
    steps: list[StepFact] = field(default_factory=list)
    tools: list[ToolFact] = field(default_factory=list)

    @property
    def success_rate(self) -> float | None:
        done = [s for s in self.steps if s.status in ("succeeded", "failed")]
        return (sum(s.status == "succeeded" for s in done) / len(done)) if done else None


def ref(kind: str, id_: uuid.UUID, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "id": str(id_), **extra}


@dataclass
class Proposal:
    rule_id: str
    rule_version: str
    candidate_type: str
    candidate: ProposedCandidate
    evidence: list[dict[str, Any]]
    strength: int  # how many pieces of evidence triggered the rule (orders proposals)
    projected: bool = False


@dataclass(frozen=True)
class Rule:
    id: str
    version: str
    candidate_type: str
    symptom: str
    action: str
    threshold: str
    fn: Callable[[TraceEvidence, AgentConfig, AutopilotPolicy, PricingTable], Proposal | None]

    def describe(self) -> dict[str, str]:
        return {
            "id": self.id,
            "version": self.version,
            "candidate_type": self.candidate_type,
            "symptom": self.symptom,
            "action": self.action,
            "threshold": self.threshold,
        }


def _proposal(
    rule: Rule,
    title: str,
    changes: list[ProposedChange],
    evidence: list[dict[str, Any]],
    benefits: list[str],
    risks: list[str],
    *,
    projected: bool = False,
) -> Proposal:
    return Proposal(
        rule_id=rule.id,
        rule_version=rule.version,
        candidate_type=rule.candidate_type,
        candidate=ProposedCandidate(title=title, changes=changes, expected_benefits=benefits, risks=risks),
        evidence=evidence[:12],
        strength=len(evidence),
        projected=projected,
    )


def _append(text: str, sentence: str) -> str:
    return f"{text.rstrip()} {sentence}".strip()


# ----------------------------------------------------------------------------- rules


def numeric_as_string(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    hits = [
        s
        for s in ev.steps
        if s.status == "failed"
        and s.error_type == "invalid_model_output"
        and re.search(r"is not of type '(number|integer)'", s.error_message)
    ]
    if len(hits) < 2 or JSON_NUMBERS_PRESENT.search(cfg.system_instructions):
        return None
    sample = re.search(r"('[^']*' is not of type '(?:number|integer)')", hits[0].error_message)
    return _proposal(
        RULES["numeric_as_string"],
        "Return numeric values as JSON numbers",
        [
            ProposedChange(
                field="system_instructions",
                value=_append(cfg.system_instructions, JSON_NUMBERS),
                reason=f"{len(hits)} steps failed the output contract because numbers were not JSON numbers"
                + (f" (e.g. {sample.group(1)})" if sample else ""),
            )
        ],
        [ref("step", s.id) for s in hits],
        ["Outputs satisfy numeric output contracts"],
        ["Prompt change could affect unrelated formatting"],
    )


def missing_required_fields(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    hits, names = [], set()
    for s in ev.steps:
        m = re.findall(r"'([A-Za-z0-9_]+)' is a required property", s.error_message)
        if s.status == "failed" and s.error_type == "invalid_model_output" and m:
            hits.append(s)
            names.update(m)
    sentence = f"Always include every required output field ({', '.join(sorted(names))}); use null when a value is not stated."
    if len(hits) < 2 or all(n in cfg.system_instructions for n in names):
        return None
    return _proposal(
        RULES["missing_required_fields"],
        "Always include required output fields",
        [
            ProposedChange(
                field="system_instructions",
                value=_append(cfg.system_instructions, sentence),
                reason=f"{len(hits)} steps omitted required fields: {', '.join(sorted(names))}",
            )
        ],
        [ref("step", s.id) for s in hits],
        ["Outputs include all required fields"],
        ["May produce null placeholders instead of failing visibly"],
    )


def _schema_guidance(schema: dict[str, Any]) -> str:
    required = set(schema.get("required") or [])
    parts = []
    for name, spec in sorted((schema.get("properties") or {}).items()):
        bits = [str(spec.get("type", "any"))]
        if spec.get("enum"):
            bits.append("one of " + "|".join(map(str, spec["enum"])))
        if spec.get("format") == "date" or "date" in name.lower():
            bits.append("ISO 8601 date YYYY-MM-DD")
        parts.append(f"{name} ({', '.join(bits)}{', required' if name in required else ''})")
    return "Argument rules: " + "; ".join(parts) + "."


def invalid_tool_arguments(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    by_tool: dict[str, list[ToolFact]] = defaultdict(list)
    for t in ev.tools:
        if (
            t.status == "failed"
            and t.error_type == "invalid_tool_arguments"
            and t.tool in cfg.allowed_tool_ids
        ):
            by_tool[t.tool].append(t)
    best = max(by_tool.items(), key=lambda kv: (len(kv[1]), kv[0]), default=None)
    if best is None or len(best[1]) < 2:
        return None
    tool_name, facts = best
    tool = get_registry().get(tool_name)
    if tool is None or "Argument rules:" in cfg.tool_descriptions.get(tool_name, ""):
        return None
    base = cfg.tool_descriptions.get(tool_name, tool.description)
    description = f"{base.rstrip()} {_schema_guidance(tool.input_schema())}"
    return _proposal(
        RULES["invalid_tool_arguments"],
        f"Spell out the argument format for {tool_name}",
        [
            ProposedChange(
                field="tool_descriptions",
                value={**cfg.tool_descriptions, tool_name: description},
                reason=f"{len(facts)} calls to {tool_name} were rejected for invalid arguments (e.g. {facts[0].error_message[:120]})",
            )
        ],
        [ref("tool_execution", t.id, tool=t.tool) for t in facts],
        [f"Fewer rejected {tool_name} calls"],
        ["Longer tool description adds a little context"],
    )


def empty_retrieval(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    searches = [
        t
        for t in ev.tools
        if t.tool == "document_search" and t.status == "succeeded" and t.result_count is not None
    ]
    empty = [t for t in searches if t.result_count == 0]
    if (
        len(empty) < 3
        or len(empty) / max(1, len(searches)) < 0.25
        or EMPTY_SEARCH_PRESENT.search(cfg.tool_strategy)
    ):
        return None
    return _proposal(
        RULES["empty_retrieval"],
        "Handle empty search results explicitly",
        [
            ProposedChange(
                field="tool_strategy",
                value=_append(cfg.tool_strategy, EMPTY_SEARCH),
                reason=f"{len(empty)} of {len(searches)} searches returned nothing",
            )
        ],
        [ref("tool_execution", t.id) for t in empty],
        ["Fewer fruitless repeated searches"],
        ["Agent may report null where a longer search would have succeeded"],
    )


def excessive_retries(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    heavy = [s for s in ev.steps if s.failures >= 2]
    if len(heavy) < 3 or (cfg.retry_max_attempts is not None and cfg.retry_max_attempts <= 2):
        return None
    return _proposal(
        RULES["excessive_retries"],
        "Bound retries at 2 attempts",
        [
            ProposedChange(
                field="retry_max_attempts",
                value=2,
                reason=f"{len(heavy)} steps failed 2 or more times before ending (retry cost without recovery)",
            )
        ],
        [ref("step", s.id, failures=s.failures) for s in heavy],
        ["Lower retry spend on persistent failures"],
        ["Transient failures that needed a third attempt will now fail"],
    )


def redundant_tool_calls(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    groups: dict[tuple[uuid.UUID, str, str], list[ToolFact]] = defaultdict(list)
    for t in ev.tools:
        if t.status == "succeeded":
            groups[(t.run_id, t.tool, t.args_hash)].append(t)
    dup = {k: v for k, v in groups.items() if len(v) >= 2}
    if len({k[0] for k in dup}) < 3 or NO_REPEAT_PRESENT.search(cfg.tool_strategy):
        return None
    evidence = [ref("tool_execution", t.id, tool=t.tool) for v in dup.values() for t in v[:2]]
    return _proposal(
        RULES["redundant_tool_calls"],
        "Protect against duplicate tool calls",
        [
            ProposedChange(
                field="tool_strategy",
                value=_append(cfg.tool_strategy, NO_REPEAT),
                reason=f"identical successful tool calls were repeated within {len({k[0] for k in dup})} runs",
            )
        ],
        evidence,
        ["Fewer redundant tool calls and lower cost"],
        ["Agent may reuse a stale result when a re-check was warranted"],
    )


def step_limit_exceeded(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    hits = [s for s in ev.steps if s.error_type == "step_limit_exceeded"]
    new = min(cfg.max_steps + 2, policy.max_steps_limit)
    if len(hits) < 2 or new <= cfg.max_steps:
        return None
    return _proposal(
        RULES["step_limit_exceeded"],
        f"Raise the step limit to {new}",
        [
            ProposedChange(
                field="max_steps",
                value=new,
                reason=f"{len(hits)} steps ended because the agent hit max_steps={cfg.max_steps}",
            )
        ],
        [ref("step", s.id) for s in hits],
        ["Multi-step tasks can finish"],
        ["Higher worst-case cost and latency per task"],
    )


def unused_tool(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    if len(ev.steps) < 10:
        return None
    used = {t.tool for t in ev.tools}
    unused = sorted(set(cfg.allowed_tool_ids) - used)
    if not unused or len(unused) == len(cfg.allowed_tool_ids):
        return None
    keep = sorted(set(cfg.allowed_tool_ids) - set(unused))
    return _proposal(
        RULES["unused_tool"],
        f"Remove unused tool{'s' if len(unused) > 1 else ''}: {', '.join(unused)}",
        [
            ProposedChange(
                field="allowed_tool_ids",
                value=keep,
                reason=f"{', '.join(unused)} never appeared in {len(ev.steps)} steps; fewer tools means a smaller attack surface",
            )
        ],
        [ref("step", s.id) for s in ev.steps[:6]],
        ["Smaller tool surface; less tool-choice confusion"],
        ["A rarely needed tool becomes unavailable"],
    )


def cheaper_model(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable
) -> Proposal | None:
    rate = ev.success_rate
    cur = pricing.lookup(cfg.provider, cfg.model)
    if cur is None or rate is None or rate < 0.9 or len(ev.steps) < 10:
        return None
    best: tuple[float, str, str] | None = None
    for m in policy.allowed_models:
        if (m.provider, m.model) == (cfg.provider, cfg.model):
            continue
        p = pricing.lookup(m.provider, m.model)
        if p is None:
            continue
        saving = 1 - float(p.input + p.output) / float(cur.input + cur.output)
        if saving >= 0.2 and (best is None or saving > best[0]):
            best = (saving, m.provider, m.model)
    if best is None:
        return None
    saving, provider, model = best
    return _proposal(
        RULES["cheaper_model"],
        f"Try the cheaper approved model {provider}/{model}",
        [
            ProposedChange(field="provider", value=provider, reason="policy-approved alternative"),
            ProposedChange(
                field="model",
                value=model,
                reason=f"projected {saving:.0%} lower list price per token (price table {pricing.version}); quality is unverified",
            ),
        ],
        [ref("step", s.id) for s in ev.steps[:6]],
        [f"Projected {saving:.0%} lower token cost (estimate, not measured)"],
        ["Quality may drop: the experiment decides"],
        projected=True,
    )


def _rule(id_: str, ctype: str, symptom: str, action: str, threshold: str, fn: Any) -> Rule:
    return Rule(id_, "1", ctype, symptom, action, threshold, fn)


RULES: dict[str, Rule] = {}
for _r in (
    _rule(
        "numeric_as_string",
        "prompt_revision",
        "Output-contract failures: a number was emitted as a string",
        "Append a JSON-number instruction to the system prompt",
        ">= 2 failed steps with \"is not of type 'number'\"",
        numeric_as_string,
    ),
    _rule(
        "missing_required_fields",
        "prompt_revision",
        "Output-contract failures: required fields missing",
        "Append an instruction to always include the named fields (null when unknown)",
        '>= 2 failed steps with "is a required property"',
        missing_required_fields,
    ),
    _rule(
        "invalid_tool_arguments",
        "tool_description_revision",
        "Tool calls rejected for invalid arguments",
        "Append the tool's argument schema (types, enums, ISO dates) to its description",
        ">= 2 rejected calls to one tool",
        invalid_tool_arguments,
    ),
    _rule(
        "empty_retrieval",
        "tool_strategy",
        "Document searches that return nothing",
        "Add an explicit empty-result strategy (retry once with the subject name, then null)",
        ">= 3 empty searches and >= 25% of searches",
        empty_retrieval,
    ),
    _rule(
        "excessive_retries",
        "retry_policy",
        "Steps failing repeatedly before ending",
        "Bound retry_max_attempts at 2",
        ">= 3 steps with >= 2 failures",
        excessive_retries,
    ),
    _rule(
        "redundant_tool_calls",
        "tool_strategy",
        "Identical successful tool calls repeated within a run",
        "Add duplicate-call protection to the tool strategy",
        "identical calls in >= 3 runs",
        redundant_tool_calls,
    ),
    _rule(
        "step_limit_exceeded",
        "step_limit",
        "Steps ending at the step limit",
        "Raise max_steps by 2 within the workspace limit",
        ">= 2 steps with step_limit_exceeded",
        step_limit_exceeded,
    ),
    _rule(
        "unused_tool",
        "tool_selection",
        "An allowed tool is never used",
        "Remove the unused tool (removal only)",
        ">= 10 steps observed, tool never called",
        unused_tool,
    ),
    _rule(
        "cheaper_model",
        "model_configuration",
        "A policy-approved model is cheaper and the agent already succeeds",
        "Propose the cheaper approved model (projected saving, unverified quality)",
        "success rate >= 90% over >= 10 steps, >= 20% lower list price",
        cheaper_model,
    ),
):
    RULES[_r.id] = _r


def catalog() -> dict[str, Any]:
    return {"version": RULESET_VERSION, "rules": [r.describe() for r in RULES.values()]}


def propose(
    ev: TraceEvidence, cfg: AgentConfig, policy: AutopilotPolicy, pricing: PricingTable | None = None
) -> list[Proposal]:
    """All rules over one evidence snapshot, strongest evidence first (ties by rule id): deterministic."""
    table = pricing or get_pricing()
    out = [p for r in RULES.values() if (p := r.fn(ev, cfg, policy, table)) is not None]
    return sorted(out, key=lambda p: (-p.strength, p.rule_id))


# ----------------------------------------------------------------------------- evidence collection


async def collect_evidence(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    *,
    since: datetime | None = None,
    version_id: uuid.UUID | None = None,
) -> TraceEvidence:
    q = (
        select(WorkflowStep)
        .join(WorkflowRun, WorkflowRun.id == WorkflowStep.run_id)
        .where(
            WorkflowRun.workspace_id == workspace_id,
            WorkflowStep.agent_definition_id == agent_id,
            WorkflowRun.purpose.not_in(EXCLUDED_PURPOSES),
        )
        .order_by(WorkflowStep.created_at)
    )
    if since:
        q = q.where(WorkflowRun.created_at >= since)
    if version_id:
        q = q.where(WorkflowStep.agent_version_id == version_id)
    steps = list((await session.execute(q)).scalars())
    ev = TraceEvidence(agent_id=agent_id, since=since)
    for s in steps:
        err = s.error or {}
        ev.steps.append(
            StepFact(
                s.id,
                s.run_id,
                s.status,
                err.get("type"),
                str(err.get("message", "")),
                s.attempt_count,
                s.failure_count,
            )
        )
    if steps:
        tes = (
            await session.execute(
                select(ToolExecution)
                .where(ToolExecution.step_id.in_([s.id for s in steps]))
                .order_by(ToolExecution.created_at)
            )
        ).scalars()
        for t in tes:
            err = t.error or {}
            count = (
                (t.result or {}).get("result_count")
                if t.tool_name == "document_search" and t.result
                else None
            )
            ev.tools.append(
                ToolFact(
                    t.id,
                    t.run_id,
                    t.step_id,
                    t.tool_name,
                    t.status,
                    err.get("type"),
                    str(err.get("message", "")),
                    t.arguments_hash,
                    count,
                )
            )
    return ev


LIVE_STATUSES = ("proposed", "evaluating", "eligible", "not_eligible", "inconclusive")


async def generate_rule_candidates(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID,
    diagnosis_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    window_days: int = 30,
    count: int | None = None,
) -> list[OptimizationCandidate]:
    """Run the rule set over the agent's recent traces and store the proposals through the normal policy checks."""
    ws = await session.get(Workspace, workspace_id)
    assert ws is not None
    policy = load_policy(ws.settings)
    agent = await session.get(AgentDefinition, agent_id)
    if agent is None or agent.workspace_id != workspace_id or agent.active_version_id is None:
        raise ValueError("agent not found or not versioned")
    baseline = await session.get(AgentConfigVersion, agent.active_version_id)
    assert baseline is not None
    if diagnosis_id is not None:
        d = await session.get(FailureDiagnosis, diagnosis_id)
        if d is None or d.agent_id != agent_id:
            raise ValueError("diagnosis must belong to this agent")
    cfg = AgentConfig.model_validate(baseline.config_snapshot)
    evidence = await collect_evidence(
        session, workspace_id, agent_id, since=utcnow() - timedelta(days=window_days)
    )
    proposals = propose(evidence, cfg, policy)[
        : min(count or policy.max_candidates, policy.max_candidates, 5)
    ]
    hidden = await _hidden_cases(session, workspace_id)
    existing = list(
        (
            await session.execute(
                select(OptimizationCandidate).where(
                    OptimizationCandidate.agent_id == agent_id,
                    OptimizationCandidate.baseline_version_id == baseline.id,
                    OptimizationCandidate.generator == "rule_engine",
                    OptimizationCandidate.status.in_(LIVE_STATUSES),
                )
            )
        ).scalars()
    )
    out: list[OptimizationCandidate] = []
    for p in proposals:
        changes = [c.model_dump(mode="json") for c in p.candidate.changes]
        if any(e.rule_id == p.rule_id and e.changes == changes for e in existing):
            continue  # this exact proposal is already open against this baseline
        out.append(
            await _store_candidate(
                session,
                workspace_id=workspace_id,
                agent_id=agent_id,
                baseline=baseline,
                proposal=p.candidate,
                policy=policy,
                hidden=hidden,
                diagnosis_id=diagnosis_id,
                generator="rule_engine",
                meta={"simulated": False},
                user_id=user_id,
                rule={
                    "rule_id": p.rule_id,
                    "rule_version": p.rule_version,
                    "candidate_type": p.candidate_type,
                    "evidence": p.evidence,
                    "projected": p.projected,
                },
            )
        )
    return out
