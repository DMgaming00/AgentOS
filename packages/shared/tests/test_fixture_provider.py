"""Stored model-response fixtures: deterministic, versioned, simulated, and never a silent fallback."""

import copy
import json

import pytest

from agentos_core.domain.errors import ProviderNotConfiguredError
from agentos_core.providers.base import Message, ModelConfig, ModelResponse, ToolCall, ToolDefinition, Usage
from agentos_core.providers.fixtures import (
    FixtureMissError,
    FixtureProvider,
    FixtureSet,
    fixture_entry,
    load_fixture_sets,
    request_digest,
)

CFG = ModelConfig(model="fixture-smoke", max_tokens=100)


def user(text):
    return [Message(role="user", text=text)]


async def test_shipped_fixture_set_matches_by_substring_and_is_labeled_simulated():
    p = FixtureProvider()
    r = await p.generate("you are fixture-smoke", user("please say hello"), [], CFG)
    assert r.text == "Hello from a stored fixture."
    assert r.provider == "fixture" and r.usage.simulated and r.model == "fixture:offline-smoke@v1"
    assert (r.usage.input_tokens, r.usage.output_tokens) == (12, 6)


async def test_tool_call_fixture_has_deterministic_ids():
    p = FixtureProvider()
    a = await p.generate("fixture-smoke", user("add 2 and 3"), [], CFG)
    b = await p.generate("fixture-smoke", user("add 2 and 3"), [], CFG)
    assert a.finish_reason == "tool_use" and a.tool_calls[0].arguments == {
        "operation": "add",
        "values": [2, 3],
    }
    assert [c.id for c in a.tool_calls] == [c.id for c in b.tool_calls] == ["fixture_calc-call_0"]


async def test_missing_fixture_fails_loudly_with_the_request_digest():
    with pytest.raises(FixtureMissError) as e:
        await FixtureProvider().generate("fixture-smoke", user("something unrecorded"), [], CFG)
    assert e.value.error_type == "fixture_missing" and len(e.value.details["request_digest"]) == 64


async def test_explicit_fixture_id_selects_the_response():
    cfg = ModelConfig(model="m", max_tokens=10, extra={"fixture_id": "greeting"})
    r = await FixtureProvider().generate("anything", user("anything"), [], cfg)
    assert r.text == "Hello from a stored fixture."
    with pytest.raises(FixtureMissError):
        await FixtureProvider().generate(
            "x", user("y"), [], ModelConfig(model="m", max_tokens=10, extra={"fixture_id": "nope"})
        )


async def test_recorded_entry_round_trips_by_exact_digest_and_is_sanitized():
    tools = [ToolDefinition("calculator", "math", {"type": "object"})]
    resp = ModelResponse(
        text="key sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456 done",
        tool_calls=[ToolCall(id="toolu_1", name="calculator", arguments={"operation": "add", "values": [1]})],
        model="claude-opus-5-5",
        usage=Usage(input_tokens=7, output_tokens=3),
        latency_ms=420,
        finish_reason="tool_use",
        provider="anthropic",
    )
    entry = fixture_entry("rec-1", "sys", user("q"), tools, CFG, resp)
    assert "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456" not in json.dumps(entry)
    fs = FixtureSet({"fixture_set": "rec", "version": 3, "simulated": True, "fixtures": [entry]})
    out = await FixtureProvider([fs]).generate("sys", user("q"), tools, CFG)
    assert out.request_id == "rec@v3:rec-1" and out.tool_calls[0].name == "calculator"
    # a different request does not match the recorded digest
    with pytest.raises(FixtureMissError):
        await FixtureProvider([fs]).generate("sys", user("q2"), tools, CFG)


def test_digest_ignores_call_ids_and_raw_payloads_but_not_content():
    a = [Message(role="assistant", tool_calls=[ToolCall("id-1", "t", {"x": 1})], raw=[{"opaque": 1}])]
    b = [Message(role="assistant", tool_calls=[ToolCall("id-2", "t", {"x": 1})], raw=None)]
    c = [Message(role="assistant", tool_calls=[ToolCall("id-1", "t", {"x": 2})])]
    assert (
        request_digest("s", a, [], "m") == request_digest("s", b, [], "m") != request_digest("s", c, [], "m")
    )


def test_fixture_sets_must_be_simulated_and_have_unique_ids():
    base = {
        "fixture_set": "x",
        "version": 1,
        "simulated": True,
        "fixtures": [{"id": "a", "match": {}, "response": {}}],
    }
    bad = copy.deepcopy(base)
    del bad["simulated"]
    with pytest.raises(ProviderNotConfiguredError):
        FixtureSet(bad)
    dup = copy.deepcopy(base)
    dup["fixtures"].append(dup["fixtures"][0])
    with pytest.raises(ProviderNotConfiguredError):
        FixtureSet(dup)


def test_shipped_sets_load_and_have_stable_digests():
    a, b = load_fixture_sets(), load_fixture_sets()
    assert [s.name for s in a] == ["offline-smoke"] and [s.digest for s in a] == [s.digest for s in b]
