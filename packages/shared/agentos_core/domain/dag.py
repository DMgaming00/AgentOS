"""Workflow DAG model, validation and input resolution.

A workflow definition is JSON validated in two passes:

1. Structural (Pydantic): types, field ranges, node-type specific fields.
2. Semantic (``validate_definition``): duplicate keys, missing dependencies,
   cycles (Kahn's algorithm), node/budget limits and tool authorization against
   the workspace's agents and the tool registry.

LLM-generated plans go through exactly the same function, so a model can never
produce a plan that bypasses policy.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

NODE_KEY_RE = r"^[a-z][a-z0-9_]{0,62}$"


class NodeType(StrEnum):
    AGENT_TASK = "agent_task"
    TOOL_TASK = "tool_task"
    APPROVAL_GATE = "approval_gate"
    AGGREGATION = "aggregation"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_base_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    backoff_max_seconds: float = Field(default=30.0, ge=0.0, le=600.0)
    jitter: bool = True
    deadline_seconds: float | None = Field(default=None, gt=0, le=24 * 3600)

    @model_validator(mode="after")
    def _check(self) -> RetryPolicy:
        if self.backoff_max_seconds < self.backoff_base_seconds:
            raise ValueError("backoff_max_seconds must be >= backoff_base_seconds")
        return self


class BudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_model_calls: int = Field(default=50, ge=1, le=1000)
    max_tool_calls: int = Field(default=100, ge=0, le=5000)
    max_duration_seconds: int = Field(default=3600, ge=10, le=24 * 3600)
    max_cost_usd: float = Field(default=5.0, ge=0.0)
    max_concurrency: int = Field(default=4, ge=1, le=64)


class NodeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=NODE_KEY_RE)
    type: NodeType
    name: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    depends_on: list[str] = Field(default_factory=list, max_length=50)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    output_contract: dict[str, Any] | None = None
    # agent_task
    agent: str | None = None
    prompt: str | None = Field(default=None, max_length=20_000)
    tools: list[str] | None = None
    # tool_task
    tool: str | None = None
    arguments: dict[str, Any] | None = None
    # approval_gate
    action: str | None = Field(default=None, max_length=500)
    reason: str | None = Field(default=None, max_length=2000)
    # aggregation
    mode: Literal["collect", "merge"] | None = None
    # free-form input passed to the node (may contain {"$ref": "..."} values)
    input: dict[str, Any] = Field(default_factory=dict)
    # deterministic fault injection used by demos and tests
    inject: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _type_specific(self) -> NodeDefinition:
        if self.type == NodeType.AGENT_TASK and not self.agent:
            raise ValueError(f"node {self.key!r}: agent_task requires 'agent'")
        if self.type == NodeType.TOOL_TASK and not self.tool:
            raise ValueError(f"node {self.key!r}: tool_task requires 'tool'")
        if self.type == NodeType.APPROVAL_GATE and not self.action:
            raise ValueError(f"node {self.key!r}: approval_gate requires 'action'")
        if self.type != NodeType.AGENT_TASK and (self.agent or self.tools):
            raise ValueError(f"node {self.key!r}: only agent_task nodes may set 'agent' or 'tools'")
        if self.type != NodeType.TOOL_TASK and (self.tool or self.arguments):
            raise ValueError(f"node {self.key!r}: only tool_task nodes may set 'tool' or 'arguments'")
        return self


class WorkflowSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    nodes: list[NodeDefinition] = Field(min_length=1)
    budget: BudgetPolicy = Field(default_factory=BudgetPolicy)
    input_schema: dict[str, Any] | None = None


# --------------------------------------------------------------------------- validation


@dataclass(frozen=True)
class AgentInfo:
    name: str
    allowed_tools: frozenset[str]


@dataclass(frozen=True)
class ToolInfo:
    name: str
    enabled: bool = True


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    node: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "node": self.node}


@dataclass
class ValidationResult:
    spec: WorkflowSpec | None
    issues: list[ValidationIssue] = field(default_factory=list)
    topological_order: list[str] = field(default_factory=list)
    levels: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.spec is not None and not self.issues


@dataclass(frozen=True)
class Limits:
    max_nodes: int = 50
    max_cost_usd: float = 25.0
    max_attempts: int = 10


def topological_sort(nodes: dict[str, list[str]]) -> tuple[list[str], set[str]]:
    """Kahn's algorithm. Returns (order, nodes_in_cycles). Unknown deps are ignored here."""
    indegree = {k: 0 for k in nodes}
    dependents: dict[str, list[str]] = {k: [] for k in nodes}
    for key, deps in nodes.items():
        for dep in deps:
            if dep in nodes:
                indegree[key] += 1
                dependents[dep].append(key)
    queue = deque(sorted(k for k, d in indegree.items() if d == 0))
    order: list[str] = []
    while queue:
        k = queue.popleft()
        order.append(k)
        for child in sorted(dependents[k]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    cyclic = {k for k, d in indegree.items() if d > 0}
    return order, cyclic


def compute_levels(order: list[str], nodes: dict[str, list[str]]) -> dict[str, int]:
    levels: dict[str, int] = {}
    for k in order:
        levels[k] = 1 + max((levels[d] for d in nodes[k] if d in levels), default=-1)
    return levels


def validate_definition(
    raw: dict[str, Any],
    *,
    agents: dict[str, AgentInfo],
    tools: dict[str, ToolInfo],
    limits: Limits = Limits(),
) -> ValidationResult:
    try:
        spec = WorkflowSpec.model_validate(raw)
    except ValidationError as exc:
        schema_issues = [
            ValidationIssue(
                code="schema",
                message=f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}",
            )
            for err in exc.errors(include_url=False)
        ]
        return ValidationResult(spec=None, issues=schema_issues)

    issues: list[ValidationIssue] = []
    if len(spec.nodes) > limits.max_nodes:
        issues.append(
            ValidationIssue("too_many_nodes", f"{len(spec.nodes)} nodes exceeds limit {limits.max_nodes}")
        )
    if spec.budget.max_cost_usd > limits.max_cost_usd:
        issues.append(
            ValidationIssue(
                "budget_exceeds_limit",
                f"max_cost_usd {spec.budget.max_cost_usd} exceeds workspace limit {limits.max_cost_usd}",
            )
        )

    graph: dict[str, list[str]] = {}
    for node in spec.nodes:
        if node.key in graph:
            issues.append(ValidationIssue("duplicate_key", f"duplicate node key {node.key!r}", node.key))
            continue
        graph[node.key] = list(node.depends_on)

    for node in spec.nodes:
        if len(set(node.depends_on)) != len(node.depends_on):
            issues.append(ValidationIssue("duplicate_dependency", "dependency listed twice", node.key))
        for dep in node.depends_on:
            if dep == node.key:
                issues.append(ValidationIssue("self_dependency", "node depends on itself", node.key))
            elif dep not in graph:
                issues.append(ValidationIssue("missing_dependency", f"unknown dependency {dep!r}", node.key))
        if node.retry.max_attempts > limits.max_attempts:
            issues.append(ValidationIssue("invalid_retry", "max_attempts exceeds limit", node.key))
        issues.extend(_check_tools(node, agents, tools))

    order, cyclic = topological_sort(graph)
    if cyclic:
        issues.append(
            ValidationIssue(
                "cycle", f"dependency cycle among nodes: {', '.join(sorted(cyclic))}", sorted(cyclic)[0]
            )
        )

    for node in spec.nodes:
        for ref in _iter_refs(node.input) + _iter_refs(node.arguments or {}):
            parts = ref.split(".")
            if parts[0] == "steps" and (len(parts) < 2 or parts[1] not in node.depends_on):
                issues.append(
                    ValidationIssue(
                        "invalid_reference", f"$ref {ref!r} must point at a declared dependency", node.key
                    )
                )
            elif parts[0] not in ("steps", "input"):
                issues.append(
                    ValidationIssue(
                        "invalid_reference", f"$ref {ref!r} must start with steps. or input.", node.key
                    )
                )

    levels = compute_levels(order, graph) if not cyclic else {}
    return ValidationResult(spec=spec, issues=issues, topological_order=order, levels=levels)


def _check_tools(
    node: NodeDefinition, agents: dict[str, AgentInfo], tools: dict[str, ToolInfo]
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if node.type == NodeType.AGENT_TASK:
        assert node.agent is not None
        agent = agents.get(node.agent)
        if agent is None:
            return [
                ValidationIssue(
                    "unknown_agent", f"agent {node.agent!r} does not exist in this workspace", node.key
                )
            ]
        for t in node.tools or []:
            if t not in tools:
                issues.append(ValidationIssue("unknown_tool", f"tool {t!r} is not registered", node.key))
            elif not tools[t].enabled:
                issues.append(ValidationIssue("tool_disabled", f"tool {t!r} is disabled", node.key))
            elif t not in agent.allowed_tools:
                issues.append(
                    ValidationIssue(
                        "unauthorized_tool", f"agent {agent.name!r} is not allowed to use {t!r}", node.key
                    )
                )
    elif node.type == NodeType.TOOL_TASK:
        assert node.tool is not None
        if node.tool not in tools:
            issues.append(ValidationIssue("unknown_tool", f"tool {node.tool!r} is not registered", node.key))
        elif not tools[node.tool].enabled:
            issues.append(ValidationIssue("tool_disabled", f"tool {node.tool!r} is disabled", node.key))
    return issues


# --------------------------------------------------------------------------- references

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.\-]+)\s*\}\}")


def _iter_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            return [value["$ref"]]
        for v in value.values():
            refs.extend(_iter_refs(v))
    elif isinstance(value, list):
        for v in value:
            refs.extend(_iter_refs(v))
    elif isinstance(value, str):
        refs.extend(_TEMPLATE_RE.findall(value))
    return refs


def _lookup(path: str, context: dict[str, Any]) -> Any:
    cur: Any = context
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            raise KeyError(path)
    return cur


def resolve_refs(value: Any, context: dict[str, Any]) -> Any:
    """Resolve ``{"$ref": "steps.x.output.y"}`` objects and ``{{input.z}}`` templates."""
    if isinstance(value, dict):
        if set(value) == {"$ref"} and isinstance(value["$ref"], str):
            return _lookup(value["$ref"], context)
        return {k: resolve_refs(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_refs(v, context) for v in value]
    if isinstance(value, str):

        def sub(m: re.Match[str]) -> str:
            found = _lookup(m.group(1), context)
            return found if isinstance(found, str) else str(found)

        return _TEMPLATE_RE.sub(sub, value)
    return value
