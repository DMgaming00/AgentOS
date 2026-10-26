"""Conditions, fan_out and sub_workflow definitions, and the scheduling score."""

import pytest

from agentos_core.domain.dag import AgentInfo, ToolInfo, evaluate_condition, validate_definition
from agentos_core.engine.core import effective_priority

TOOLS = {"calculator": ToolInfo("calculator")}
AGENTS = {"A": AgentInfo("A", frozenset({"calculator"}))}
CALC = {"type": "tool_task", "tool": "calculator", "arguments": {"operation": "add", "values": [1, 2]}}


def check(nodes):
    return validate_definition({"nodes": nodes}, agents=AGENTS, tools=TOOLS)


def codes(result):
    return {i.code for i in result.issues}


@pytest.mark.parametrize(
    "op,value,actual,expected",
    [
        ("eq", 3, 3, True),
        ("eq", 3, 4, False),
        ("ne", 3, 4, True),
        ("gt", 3, 4, True),
        ("gt", 3, 3, False),
        ("gte", 3, 3, True),
        ("lt", 3, 2, True),
        ("lte", 3, 3, True),
        ("in", [1, 2], 2, True),
        ("in", [1, 2], 3, False),
        ("not_in", [1, 2], 3, True),
        ("contains", "b", "abc", True),
        ("contains", "z", "abc", False),
        ("truthy", None, 0, False),
        ("truthy", None, "x", True),
        ("exists", None, 0, True),
        ("exists", None, None, False),
    ],
)
def test_condition_operators(op, value, actual, expected):
    cond = {"ref": "input.v", "op": op, "value": value}
    assert evaluate_condition(cond, {"input": {"v": actual}}) is expected


def test_unresolvable_or_mistyped_conditions_are_false_not_errors():
    ctx = {"input": {"v": "text"}}
    assert evaluate_condition({"ref": "input.missing", "op": "eq", "value": 1}, ctx) is False
    assert (
        evaluate_condition({"ref": "input.missing", "op": "ne", "value": 1}, ctx) is False
    )  # unknown != anything
    assert evaluate_condition({"ref": "input.missing", "op": "exists"}, ctx) is False
    assert evaluate_condition({"ref": "input.v", "op": "gt", "value": 3}, ctx) is False  # str > int
    assert (
        evaluate_condition(
            {"ref": "steps.a.output.x", "op": "eq", "value": 1}, {"steps": {"a": {"output": {"x": 1}}}}
        )
        is True
    )


def test_valid_branching_fan_out_and_sub_workflow_definition():
    r = check(
        [
            {"key": "prep", "type": "aggregation"},
            {
                "key": "fast",
                "type": "aggregation",
                "depends_on": ["prep"],
                "when": {"ref": "steps.prep.output.n", "op": "gt", "value": 1},
            },
            {
                "key": "each",
                "type": "fan_out",
                "depends_on": ["prep"],
                "over": "steps.prep.output.items",
                "max_items": 5,
                "template": {
                    **CALC,
                    "arguments": {"operation": "add", "values": [{"$ref": "item.a"}, {"$ref": "index"}]},
                },
            },
            {
                "key": "sub",
                "type": "sub_workflow",
                "workflow": "child",
                "depends_on": ["each"],
                "input": {"v": {"$ref": "steps.each.output.count"}},
                "trigger": "any_succeeded",
            },
        ]
    )
    assert r.ok, r.issues


@pytest.mark.parametrize(
    "node,code",
    [
        ({"key": "f", "type": "fan_out", "over": "input.x"}, "schema"),  # template missing
        ({"key": "f", "type": "fan_out", "template": CALC}, "schema"),  # over missing
        (
            {
                "key": "f",
                "type": "fan_out",
                "over": "input.x",
                "template": {"type": "approval_gate", "action": "x"},
            },
            "schema",
        ),
        (
            {"key": "f", "type": "fan_out", "over": "input.x", "template": {**CALC, "depends_on": ["a"]}},
            "schema",
        ),
        ({"key": "f", "type": "fan_out", "over": "input.x", "max_items": 21, "template": CALC}, "schema"),
        (
            {
                "key": "f",
                "type": "fan_out",
                "over": "input.x",
                "template": {"type": "tool_task", "tool": "nope"},
            },
            "unknown_tool",
        ),
        (
            {"key": "f", "type": "fan_out", "over": "steps.ghost.output.x", "template": CALC},
            "invalid_reference",
        ),
        ({"key": "f", "type": "tool_task", "tool": "calculator", "over": "input.x"}, "schema"),
        ({"key": "s", "type": "sub_workflow"}, "schema"),
        ({"key": "s", "type": "aggregation", "workflow": "x"}, "schema"),
        (
            {
                "key": "n",
                "type": "aggregation",
                "when": {"ref": "steps.ghost.output.x", "op": "eq", "value": 1},
            },
            "invalid_reference",
        ),
        (
            {"key": "n", "type": "aggregation", "when": {"ref": "input.x", "op": "regex", "value": "x"}},
            "schema",
        ),
        (
            {
                "key": "n",
                "type": "tool_task",
                "tool": "calculator",
                "arguments": {"operation": "add", "values": [{"$ref": "item.a"}]},
            },
            "invalid_reference",
        ),  # item only in templates
    ],
)
def test_invalid_definitions_are_rejected(node, code):
    assert code in codes(check([node]))


def test_template_may_not_use_an_unauthorized_tool_or_agent():
    r = check(
        [
            {
                "key": "f",
                "type": "fan_out",
                "over": "input.x",
                "template": {"type": "agent_task", "agent": "A", "tools": ["document_search"]},
            }
        ]
    )
    assert "unauthorized_tool" in codes(r) or "unknown_tool" in codes(r)
    r = check(
        [
            {
                "key": "f",
                "type": "fan_out",
                "over": "input.x",
                "template": {"type": "agent_task", "agent": "Nobody"},
            }
        ]
    )
    assert "unknown_agent" in codes(r)


def test_effective_priority_ages_waiting_work():
    assert effective_priority(5, 0, 30) == 5
    assert effective_priority(0, 300, 30) == 10  # a priority-0 step that waited 10 intervals
    assert effective_priority(0, 300, 30) > effective_priority(9, 0, 30)  # outranks fresh top priority
    assert effective_priority(3, -5, 30) == 3  # clock skew never lowers priority
