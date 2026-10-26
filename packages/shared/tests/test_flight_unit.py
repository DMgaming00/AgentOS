"""Flight Recorder pure logic: sanitized capture, structured diff, state reconstruction, fail-closed export."""

import json

import pytest

from agentos_core.flight import export as export_mod
from agentos_core.flight.capture import MAX_REQUEST_BYTES, capture, sanitize_messages
from agentos_core.flight.diff import diff_recordings, json_diff
from agentos_core.flight.export import ExportRefusedError, build_export
from agentos_core.flight.timeline import canonical_hash, state_at
from agentos_core.providers.base import (
    Message,
    ModelConfig,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    ToolResult,
    Usage,
)

SECRET = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789"
CFG = ModelConfig(model="m", max_tokens=100)


def test_capture_redacts_secrets_everywhere_and_drops_provider_raw_payloads():
    msgs = [
        Message(role="user", text=f"use key {SECRET} please"),
        Message(
            role="assistant",
            text="ok",
            tool_calls=[ToolCall("c1", "calculator", {"api_key": SECRET, "v": 1})],
            raw=[{"type": "thinking", "signature": "opaque-signature"}],
        ),
        Message(
            role="tool_results",
            tool_results=[ToolResult("c1", "calculator", f"Bearer {'x' * 30} and {SECRET}")],
        ),
    ]
    resp = ModelResponse(
        "done", [ToolCall("c2", "t", {"password": "hunter2"})], "m", Usage(1, 2), 5, "end_turn", "mock"
    )
    body = capture(
        "rid",
        f"system {SECRET}",
        msgs,
        [ToolDefinition("calculator", "math", {"type": "object"})],
        CFG,
        meta={"agent": "A"},
        response=resp,
    )
    blob = json.dumps({"req": body.request, "resp": body.response})
    assert SECRET not in blob and "hunter2" not in blob and "opaque-signature" not in blob
    assert "[REDACTED]" in blob and body.response["usage"]["input_tokens"] == 1
    # the digest identifies the *real* request, so replay can detect divergence exactly
    other = capture("rid2", "system different", msgs, [], CFG, meta={})
    assert body.request_digest != other.request_digest


def test_oversize_requests_keep_first_and_recent_messages_and_say_so():
    msgs = [Message(role="user", text="x" * 15_000)] + [
        Message(role="assistant" if i % 2 else "user", text="y" * 15_000) for i in range(40)
    ]
    body = capture("r", "s", msgs, [], CFG, meta={})
    assert body.truncated and len(json.dumps(body.request)) < MAX_REQUEST_BYTES * 1.5
    roles = [m["role"] for m in body.request["messages"]]
    assert roles[0] == "user" and "omitted" in roles and body.request["messages"][1]["count"] > 0
    assert len(sanitize_messages(msgs)) == 41  # sanitizing itself never drops messages


def rec(steps, calls=(), tools=(), output=None, status="succeeded", rid="r"):
    return {
        "run": {
            "id": rid,
            "status": status,
            "output": output,
            "duration_ms": 100,
            "cost_spent_usd": "0.1",
            "model_calls": len(calls),
            "tool_calls": len(tools),
        },
        "steps": [
            {
                "key": k,
                "status": s,
                "attempts": a,
                "failures": f,
                "output": o,
                "started_ms": i,
                "duration_ms": 5,
            }
            for i, (k, s, a, f, o) in enumerate(steps)
        ],
        "model_calls": list(calls),
        "tool_executions": list(tools),
    }


def call(step, digest, tok=10):
    return {
        "id": f"{step}-{digest}",
        "step_key": step,
        "status": "succeeded",
        "request_digest": digest,
        "latency_ms": 5,
        "input_tokens": tok,
        "output_tokens": tok,
        "model": "m",
    }


def tool(step, name, h, status="succeeded"):
    return {"step_key": step, "tool": name, "arguments_hash": h * 12, "status": status}


def test_diff_finds_branch_retry_tool_model_and_output_differences():
    a = rec(
        [
            ("prep", "succeeded", 1, 0, {"r": 2}),
            ("fast", "succeeded", 1, 0, {"x": 1}),
            ("slow", "skipped", 0, 0, None),
            ("agent", "succeeded", 1, 0, {"answer": "A"}),
        ],
        calls=[call("agent", "d1")],
        tools=[tool("agent", "search", "a")],
        output={"answer": "A"},
    )
    b = rec(
        [
            ("prep", "succeeded", 1, 0, {"r": 2}),
            ("fast", "skipped", 0, 0, None),
            ("slow", "succeeded", 2, 1, {"x": 9}),
            ("agent", "succeeded", 1, 0, {"answer": "B"}),
        ],
        calls=[call("agent", "d2")],
        tools=[tool("agent", "calculator", "b")],
        output={"answer": "B"},
        rid="r2",
    )
    bodies_a = {"agent-d1": {"response": {"text": "alpha", "tool_calls": []}}}
    bodies_b = {"agent-d2": {"response": {"text": "beta", "tool_calls": []}}}
    d = diff_recordings(a, b, bodies_a, bodies_b, evaluation={"passed": [True, False]})
    s = d["summary"]
    assert s["identical"] is False and s["final_output_same"] is False
    assert s["final_output_diff"] == [{"path": "answer", "a": '"A"', "b": '"B"', "kind": "changed"}]
    assert set(s["branch_differences"]) == {"fast", "slow"} and s["retry_differences"] == ["slow"]
    assert s["evaluation"]["passed"] == [True, False]
    by = {x["key"]: x for x in d["steps"]}
    assert by["prep"]["differences"] == []
    kinds = {x["kind"] for x in by["agent"]["differences"]}
    assert kinds == {"output", "model_response", "tool_calls"}
    assert (
        by["agent"]["model_calls"][0]["request_same"] is False
        and by["agent"]["model_calls"][0]["a"]["text"] == "alpha"
    )
    assert by["agent"]["tool_calls"] == {"a": ["search:succeeded"], "b": ["calculator:succeeded"]}


def test_identical_recordings_report_identical():
    base = rec(
        [("a", "succeeded", 1, 0, {"v": 1})],
        calls=[call("a", "d")],
        tools=[tool("a", "t", "h")],
        output={"v": 1},
    )
    other = rec(
        [("a", "succeeded", 1, 0, {"v": 1})],
        calls=[call("a", "d")],
        tools=[tool("a", "t", "h")],
        output={"v": 1},
        rid="r2",
    )
    bodies = {"a-d": {"response": {"text": "same", "tool_calls": []}}}
    assert diff_recordings(base, other, bodies, bodies)["summary"]["identical"] is True


def test_steps_present_in_only_one_run_are_reported():
    d = diff_recordings(
        rec([("a", "succeeded", 1, 0, None)]),
        rec([("a", "succeeded", 1, 0, None), ("b", "succeeded", 1, 0, None)], rid="r2"),
    )
    only = [s for s in d["steps"] if s["presence"] != "both"]
    assert [(s["key"], s["presence"]) for s in only] == [("b", "only_b")]


def test_json_diff_paths_and_limit():
    assert json_diff({"a": [1, 2], "b": 1}, {"a": [1, 3, 4], "c": 2}) == [
        {"path": "a[1]", "a": "2", "b": "3", "kind": "changed"},
        {"path": "a[2]", "a": None, "b": "4", "kind": "added"},
        {"path": "b", "a": "1", "b": None, "kind": "removed"},
        {"path": "c", "a": None, "b": "2", "kind": "added"},
    ]
    assert len(json_diff(list(range(100)), [x + 1 for x in range(100)], limit=7)) == 7


def test_state_at_applies_deltas_up_to_a_frame():
    r = {
        "initial_status": {"a": "pending", "b": "pending"},
        "frames": [
            {"seq": 1, "status_delta": {"a": "ready"}},
            {"seq": 2, "status_delta": {"a": "running"}},
            {"seq": 3, "status_delta": {"a": "succeeded", "b": "ready"}},
        ],
    }
    assert state_at(r, 0) == {"a": "pending", "b": "pending"}
    assert state_at(r, 2) == {"a": "running", "b": "pending"}
    assert state_at(r, 99) == {"a": "succeeded", "b": "ready"}


def minimal_recording(text):
    return {
        "schema_version": 1,
        "run": {"id": "r", "input": {"note": text}},
        "steps": [],
        "integrity": {"sha256": "x"},
        "frames": [],
    }


def test_export_redacts_and_is_hash_verified():
    pkg = build_export(
        minimal_recording(f"my key is {SECRET}"), {"c": {"request": {"system": f"k={SECRET}"}}}
    )
    assert SECRET not in json.dumps(pkg) and pkg["sanitized"] is True
    assert pkg["integrity"]["package_sha256"] == canonical_hash(
        {k: v for k, v in pkg.items() if k != "integrity"}
    )


def test_export_fails_closed_if_redaction_ever_misses_something(monkeypatch):
    monkeypatch.setattr(export_mod, "redact", lambda v: v)  # simulate a redaction bug
    with pytest.raises(ExportRefusedError):
        build_export(minimal_recording(f"leaked {SECRET}"), {})
    with pytest.raises(ExportRefusedError):
        build_export({**minimal_recording("x"), "run": {"id": "r", "api_key": "plain-text-value"}}, {})
