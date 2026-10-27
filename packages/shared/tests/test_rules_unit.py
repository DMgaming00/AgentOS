"""The rule engine as pure functions: each rule fires on its symptom, stays silent without it, and is deterministic."""

import uuid

import pytest

from agentos_core.autopilot import rules
from agentos_core.autopilot.config import AgentConfig
from agentos_core.autopilot.optimizer import validate_candidate
from agentos_core.autopilot.policy import AllowedModel, AutopilotPolicy
from agentos_core.pricing import PricingTable

POLICY = AutopilotPolicy(allowed_models=[AllowedModel(provider="mock", model="mock-cheap")])
PRICING = PricingTable(
    {
        "version": "test",
        "models": {
            "mock/mock-big": {"input": 4.0, "output": 20.0, "simulated": True},
            "mock/mock-cheap": {"input": 0.1, "output": 0.5, "simulated": True},
        },
    }
)


def cfg(**kw):
    base = dict(
        system_instructions="You extract metrics.",
        provider="mock",
        model="mock-big",
        model_configuration={"scenario": "extractor"},
        allowed_tool_ids=["document_search", "structured_data_reader"],
        max_steps=4,
        max_cost_usd="0.5",
        timeout_seconds=60,
    )
    return AgentConfig.model_validate({**base, **kw})


def step(status="succeeded", etype=None, msg="", attempts=1, failures=0, run=None):
    return rules.StepFact(uuid.uuid4(), run or uuid.uuid4(), status, etype, msg, attempts, failures)


def tool(
    name="document_search", status="succeeded", etype=None, msg="", h="h", run=None, count=None, replays=0
):
    return rules.ToolFact(
        uuid.uuid4(), run or uuid.uuid4(), uuid.uuid4(), name, status, etype, msg, h, count, replays
    )


def evidence(steps=(), tools=()):
    return rules.TraceEvidence(agent_id=uuid.uuid4(), since=None, steps=list(steps), tools=list(tools))


def by_id(ev, c=None, policy=POLICY):
    return {p.rule_id: p for p in rules.propose(ev, c or cfg(), policy, PRICING)}


def test_silent_without_evidence():
    ev = evidence(steps=[step() for _ in range(12)], tools=[tool(h=str(i)) for i in range(12)])
    assert set(by_id(ev)) <= {
        "unused_tool",
        "cheaper_model",
    }  # only the rules whose symptom is "nothing went wrong"
    assert by_id(evidence()) == {}


def test_numeric_as_string_fires_on_two_failures_and_cites_them():
    bad = [
        step(
            "failed",
            "invalid_model_output",
            "output does not match contract: '$0.95' is not of type 'number'",
        )
        for _ in range(2)
    ]
    p = by_id(evidence(steps=bad))["numeric_as_string"]
    assert p.candidate_type == "prompt_revision" and p.strength == 2
    assert {e["id"] for e in p.evidence} == {str(s.id) for s in bad}
    assert p.candidate.changes[0].value.endswith("no units or currency symbols.")
    assert "'$0.95' is not of type 'number'" in p.candidate.changes[0].reason
    assert "numeric_as_string" not in by_id(evidence(steps=bad[:1]))  # one failure is not a pattern
    already = cfg(system_instructions="Return plain JSON numbers.")
    assert "numeric_as_string" not in by_id(
        evidence(steps=bad), already
    )  # does not re-propose what is already there


def test_missing_required_fields_names_the_fields():
    bad = [
        step("failed", "invalid_model_output", f"'{f}' is a required property")
        for f in ("p95_ms", "p95_ms", "price")
    ]
    p = by_id(evidence(steps=bad))["missing_required_fields"]
    assert "p95_ms, price" in p.candidate.changes[0].value


def test_invalid_tool_arguments_adds_the_schema_to_that_tools_description():
    bad = [
        tool(
            "document_search",
            "failed",
            "invalid_tool_arguments",
            "limit: 99 is greater than the maximum of 20",
        )
        for _ in range(2)
    ]
    p = by_id(evidence(tools=bad))["invalid_tool_arguments"]
    desc = p.candidate.changes[0].value["document_search"]
    assert "Argument rules:" in desc and "query (string, required)" in desc
    assert p.candidate_type == "tool_description_revision"
    unknown = [tool("calculator", "failed", "invalid_tool_arguments") for _ in range(3)]
    assert "invalid_tool_arguments" not in by_id(
        evidence(tools=unknown)
    )  # calculator is not allowed for this agent


def test_empty_retrieval_needs_volume_and_share():
    empties = [tool(count=0, h=str(i)) for i in range(3)]
    hits = [tool(count=3, h=f"x{i}") for i in range(3)]
    assert "empty_retrieval" in by_id(evidence(tools=empties + hits))  # 3 of 6
    assert "empty_retrieval" not in by_id(evidence(tools=empties + hits * 4))  # 3 of 15 is 20%
    assert "empty_retrieval" not in by_id(evidence(tools=empties[:2]))


def test_excessive_retries_bounds_at_two_unless_already_bounded():
    heavy = [step("succeeded", failures=3, attempts=4) for _ in range(3)]
    p = by_id(evidence(steps=heavy))["excessive_retries"]
    assert p.candidate.changes[0].field == "retry_max_attempts" and p.candidate.changes[0].value == 2
    assert "excessive_retries" not in by_id(evidence(steps=heavy), cfg(retry_max_attempts=2))


def test_redundant_calls_must_span_runs():
    runs = [uuid.uuid4() for _ in range(3)]
    dup = [tool(h="same", run=r) for r in runs for _ in range(2)]
    assert "redundant_tool_calls" in by_id(evidence(tools=dup))
    one_run = [tool(h="same", run=runs[0]) for _ in range(6)]
    assert "redundant_tool_calls" not in by_id(evidence(tools=one_run))


def test_redundancy_the_platform_answered_from_the_record_still_counts_as_evidence():
    # A repeated identical call is replayed (no duplicate side effect) and leaves no second execution row; the replay
    # counter is the only trace that the model asked again, and the rule must read it.
    asked_twice = [tool(h=f"h{i}", replays=1) for i in range(3)]
    p = by_id(evidence(tools=asked_twice))["redundant_tool_calls"]
    assert p.strength == 3 and p.evidence[0]["replays"] == 1
    assert "redundant_tool_calls" not in by_id(
        evidence(tools=[tool(h=f"h{i}") for i in range(3)])
    )  # no repeats, no rule


def test_step_limit_respects_the_workspace_limit():
    hits = [step("failed", "step_limit_exceeded") for _ in range(2)]
    assert by_id(evidence(steps=hits))["step_limit_exceeded"].candidate.changes[0].value == 6
    assert "step_limit_exceeded" not in by_id(evidence(steps=hits), cfg(max_steps=12))


def test_unused_tool_removal_only_and_never_empties_the_list():
    steps = [step() for _ in range(10)]
    p = by_id(evidence(steps=steps, tools=[tool("document_search")]))["unused_tool"]
    assert p.candidate.changes[0].value == ["document_search"]
    assert "unused_tool" not in by_id(
        evidence(steps=steps)
    )  # no tool was used at all: not evidence of "unused"
    assert "unused_tool" not in by_id(evidence(steps=steps[:5], tools=[tool()]))  # too little evidence


def test_cheaper_model_is_labeled_projected_and_needs_a_healthy_agent():
    healthy = evidence(steps=[step() for _ in range(10)])
    p = by_id(healthy)["cheaper_model"]
    assert (
        p.projected is True
        and "projected" in p.candidate.changes[1].reason
        and p.candidate_type == "model_configuration"
    )
    sick = evidence(
        steps=[step("failed", "invalid_model_output", "x") for _ in range(5)] + [step() for _ in range(5)]
    )
    assert "cheaper_model" not in by_id(sick)
    assert "cheaper_model" not in by_id(healthy, policy=AutopilotPolicy())  # no approved alternative


def test_every_proposal_passes_policy_validation_or_says_why():
    bad = [step("failed", "invalid_model_output", "'$1' is not of type 'number'") for _ in range(3)]
    base = cfg()
    for p in rules.propose(evidence(steps=bad + [step() for _ in range(8)]), base, POLICY, PRICING):
        new, violations = validate_candidate(base, p.candidate.changes, POLICY)
        assert new is not None or violations, p.rule_id
    # the policy, not the rule, decides: a model outside the approved list is rejected
    outside = rules.propose(evidence(steps=[step() for _ in range(10)]), base, POLICY, PRICING)
    outside_change = [c for p in outside for c in p.candidate.changes if c.field == "model"]
    assert outside_change and validate_candidate(base, outside_change, POLICY)[0] is not None


def test_deterministic_and_ordered_by_evidence_strength():
    bad = [step("failed", "invalid_model_output", "'$1' is not of type 'number'") for _ in range(2)]
    heavy = [step("succeeded", failures=2) for _ in range(5)]
    ev = evidence(steps=bad + heavy)
    a, b = rules.propose(ev, cfg(), POLICY, PRICING), rules.propose(ev, cfg(), POLICY, PRICING)
    assert [(p.rule_id, p.candidate.model_dump_json()) for p in a] == [
        (p.rule_id, p.candidate.model_dump_json()) for p in b
    ]
    assert [p.strength for p in a] == sorted((p.strength for p in a), reverse=True)


def test_catalog_documents_every_rule():
    cat = rules.catalog()
    assert cat["version"] == rules.RULESET_VERSION and len(cat["rules"]) == len(rules.RULES) == 9
    assert all(r["symptom"] and r["action"] and r["threshold"] for r in cat["rules"])
    with pytest.raises(KeyError):
        rules.RULES["random_pick"]
