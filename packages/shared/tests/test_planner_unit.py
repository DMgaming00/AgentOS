import json

import pytest

from agentos_core.domain.dag import AgentInfo, Limits, ToolInfo
from agentos_core.planner import extract_json, plan_workflow
from agentos_core.providers.base import ModelConfig
from agentos_core.providers.mock import MockProvider

AGENTS = {
    "Research Agent": AgentInfo("Research Agent", frozenset({"document_search"})),
    "Analysis Agent": AgentInfo("Analysis Agent", frozenset({"calculator"})),
    "Writer Agent": AgentInfo("Writer Agent", frozenset({"artifact_writer"})),
}
TOOLS = {n: ToolInfo(n) for n in ("document_search", "calculator", "artifact_writer")}
CATALOG = {
    "documents": [
        {"filename": f"platform-{x}.md", "title": x, "content_type": "text/markdown"} for x in "abc"
    ]
}


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('Here:\n```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('prefix {"a": 3} suffix') == {"a": 3}
    with pytest.raises(ValueError):
        extract_json("no json")


@pytest.mark.parametrize(
    ("scenario", "attempts", "ok"),
    [("planner", 1, True), ("invalid_then_valid", 2, True), ("invalid_plan", 3, False)],
)
async def test_planning_is_validated_and_bounded(scenario, attempts, ok):
    outcome = await plan_workflow(
        task="compare",
        catalog=CATALOG,
        agents=AGENTS,
        tools=TOOLS,
        limits=Limits(),
        config=ModelConfig(model="mock-planner", extra={"scenario": scenario}),
        provider=MockProvider(),
    )
    assert outcome.ok is ok and len(outcome.attempts) == attempts
    if not ok:
        assert any(i["code"] == "cycle" for i in outcome.attempts[-1].issues)
    else:
        keys = [n.key for n in outcome.result.spec.nodes]
        assert keys[-1] == "write_report" and len([k for k in keys if k.startswith("research_")]) == 3


async def test_planner_cannot_grant_unauthorized_tools():
    class Evil(MockProvider):
        def _planner(self, messages, tools, config):
            return json.dumps(
                {
                    "nodes": [
                        {
                            "key": "x",
                            "type": "agent_task",
                            "agent": "Research Agent",
                            "tools": ["artifact_writer"],
                        }
                    ]
                }
            ), []

    outcome = await plan_workflow(
        task="t",
        catalog=CATALOG,
        agents=AGENTS,
        tools=TOOLS,
        limits=Limits(),
        config=ModelConfig(model="mock-planner", extra={"scenario": "planner"}),
        provider=Evil(),
        max_attempts=2,
    )
    assert not outcome.ok and outcome.attempts[-1].issues[0]["code"] == "unauthorized_tool"
