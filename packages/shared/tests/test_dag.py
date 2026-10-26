import pytest

from agentos_core.domain.dag import (
    AgentInfo,
    Limits,
    ToolInfo,
    compute_levels,
    resolve_refs,
    topological_sort,
    validate_definition,
)

AGENTS = {
    "A": AgentInfo("A", frozenset({"calculator", "document_search"})),
    "W": AgentInfo("W", frozenset({"artifact_writer"})),
}
TOOLS = {
    n: ToolInfo(n) for n in ("calculator", "document_search", "artifact_writer", "structured_data_reader")
}


def node(key, deps=(), **kw):
    return {"key": key, "type": "agent_task", "agent": "A", "depends_on": list(deps), **kw}


def codes(result):
    return {i.code for i in result.issues}


def test_valid_dag_levels_and_order():
    r = validate_definition(
        {"nodes": [node("a"), node("b", ["a"]), node("c", ["a"]), node("d", ["b", "c"])]},
        agents=AGENTS,
        tools=TOOLS,
    )
    assert r.ok
    assert r.topological_order == ["a", "b", "c", "d"]
    assert r.levels == {"a": 0, "b": 1, "c": 1, "d": 2}


def test_cycle_detected_with_kahn():
    r = validate_definition(
        {"nodes": [node("a", ["c"]), node("b", ["a"]), node("c", ["b"])]}, agents=AGENTS, tools=TOOLS
    )
    assert "cycle" in codes(r)
    order, cyclic = topological_sort({"a": ["c"], "b": ["a"], "c": ["b"], "x": []})
    assert order == ["x"] and cyclic == {"a", "b", "c"}


def test_self_dependency_and_missing_and_duplicate():
    r = validate_definition(
        {"nodes": [node("a", ["a"]), node("b", ["zzz"]), node("b")]}, agents=AGENTS, tools=TOOLS
    )
    assert {"self_dependency", "missing_dependency", "duplicate_key"} <= codes(r)


def test_unauthorized_unknown_tools_and_agents():
    r = validate_definition(
        {
            "nodes": [
                node("a", tools=["artifact_writer"]),
                node("b", tools=["shell"]),
                {"key": "c", "type": "agent_task", "agent": "Nobody"},
            ]
        },
        agents=AGENTS,
        tools=TOOLS,
    )
    assert {"unauthorized_tool", "unknown_tool", "unknown_agent"} <= codes(r)


def test_unsupported_type_and_schema_errors():
    r = validate_definition({"nodes": [{"key": "a", "type": "shell_task"}]}, agents=AGENTS, tools=TOOLS)
    assert not r.ok and codes(r) == {"schema"}
    r = validate_definition(
        {"nodes": [{"key": "Bad Key!", "type": "aggregation"}]}, agents=AGENTS, tools=TOOLS
    )
    assert codes(r) == {"schema"}


def test_limits_nodes_budget_retry():
    nodes = [node(f"n{i}") for i in range(6)]
    r = validate_definition(
        {"nodes": nodes, "budget": {"max_cost_usd": 100}},
        agents=AGENTS,
        tools=TOOLS,
        limits=Limits(max_nodes=5, max_cost_usd=10),
    )
    assert {"too_many_nodes", "budget_exceeds_limit"} <= codes(r)
    r = validate_definition({"nodes": [node("a", retry={"max_attempts": 0})]}, agents=AGENTS, tools=TOOLS)
    assert codes(r) == {"schema"}
    r = validate_definition(
        {"nodes": [node("a", retry={"backoff_base_seconds": 10, "backoff_max_seconds": 1})]},
        agents=AGENTS,
        tools=TOOLS,
    )
    assert codes(r) == {"schema"}


def test_refs_must_target_declared_dependencies():
    r = validate_definition(
        {"nodes": [node("a"), node("b", input={"x": {"$ref": "steps.a.output"}})]}, agents=AGENTS, tools=TOOLS
    )
    assert "invalid_reference" in codes(r)
    r = validate_definition(
        {"nodes": [node("a"), node("b", ["a"], input={"x": {"$ref": "steps.a.output"}})]},
        agents=AGENTS,
        tools=TOOLS,
    )
    assert r.ok


def test_type_specific_fields():
    r = validate_definition({"nodes": [{"key": "t", "type": "tool_task"}]}, agents=AGENTS, tools=TOOLS)
    assert codes(r) == {"schema"}
    r = validate_definition(
        {"nodes": [{"key": "t", "type": "tool_task", "tool": "calculator", "agent": "A"}]},
        agents=AGENTS,
        tools=TOOLS,
    )
    assert codes(r) == {"schema"}


def test_resolve_refs_and_templates():
    ctx = {"input": {"topic": "GPUs"}, "steps": {"a": {"output": {"rows": [1, 2], "n": 3}}}}
    assert resolve_refs(
        {"x": {"$ref": "steps.a.output.rows"}, "y": "About {{input.topic}} ({{steps.a.output.n}})"}, ctx
    ) == {"x": [1, 2], "y": "About GPUs (3)"}
    with pytest.raises(KeyError):
        resolve_refs({"$ref": "steps.b.output"}, ctx)


def test_compute_levels_diamond():
    g = {"a": [], "b": ["a"], "c": ["a", "b"]}
    order, _ = topological_sort(g)
    assert compute_levels(order, g) == {"a": 0, "b": 1, "c": 2}
