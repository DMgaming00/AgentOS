"""Provider adapters exercised through the real SDK clients with a mocked HTTP transport.

These tests verify request construction and response normalization without
network access. They do NOT prove compatibility with the live APIs; see the
opt-in live smoke tests (AGENTOS_LIVE_TESTS=1) for that.
"""

import json

import anthropic
import httpx2
import openai
import pytest

from agentos_core.domain import errors as E
from agentos_core.providers.anthropic import AnthropicProvider
from agentos_core.providers.base import Message, ModelConfig, ToolCall, ToolDefinition, ToolResult
from agentos_core.providers.mock import MockProvider
from agentos_core.providers.openai import OpenAIProvider

TOOLS = [
    ToolDefinition("calculator", "math", {"type": "object", "properties": {"operation": {"type": "string"}}})
]


def anthropic_client(handler):
    return anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def openai_client(handler):
    return openai.AsyncOpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


ANTHROPIC_TOOL_RESPONSE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig-abc"},
        {"type": "text", "text": "Let me compute."},
        {"type": "tool_use", "id": "toolu_1", "name": "calculator", "input": {"operation": "mean"}},
    ],
    "stop_reason": "tool_use",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 120,
        "output_tokens": 30,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": None,
    },
}


async def test_anthropic_request_and_normalization():
    seen = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx2.Response(200, json=ANTHROPIC_TOOL_RESPONSE, headers={"request-id": "req_123"})

    p = AnthropicProvider(None, client=anthropic_client(handler))
    history = [
        Message(role="user", text="hi"),
        Message(
            role="assistant",
            provider="anthropic",
            raw=[
                {"type": "thinking", "thinking": "", "signature": "s0"},
                {"type": "tool_use", "id": "toolu_0", "name": "calculator", "input": {}},
            ],
            tool_calls=[ToolCall("toolu_0", "calculator", {})],
        ),
        Message(role="tool_results", tool_results=[ToolResult("toolu_0", "calculator", '{"result": 1}')]),
    ]
    resp = await p.generate("sys", history, TOOLS, ModelConfig(model="claude-opus-5-5", effort="low"))
    body = seen["body"]
    assert seen["url"].endswith("/v1/messages?beta=true") or seen["url"].endswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5" and body["system"] == "sys" and body["max_tokens"] == 16000
    assert body["output_config"] == {"effort": "low"}
    assert (
        body["fallbacks"] == "default"
        and "server-side-fallback-2026-07-01" in seen["headers"]["anthropic-beta"]
    )
    assert "temperature" not in body and "thinking" not in body
    assert body["tools"][0]["name"] == "calculator" and "input_schema" in body["tools"][0]
    # thinking block replayed unchanged; tool results grouped in one user message
    assert body["messages"][1]["content"][0] == {"type": "thinking", "thinking": "", "signature": "s0"}
    assert body["messages"][2]["content"][0]["type"] == "tool_result"
    assert resp.text == "Let me compute." and resp.finish_reason == "tool_use"
    assert resp.tool_calls[0].name == "calculator" and resp.tool_calls[0].arguments == {"operation": "mean"}
    assert resp.usage.input_tokens == 120 and resp.usage.cached_input_tokens == 50
    assert resp.usage.cache_write_tokens is None  # unknown stays unknown
    assert resp.raw[0]["type"] == "thinking" and resp.provider == "anthropic"


async def test_anthropic_no_fallback_for_unsupported_model():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json={**ANTHROPIC_TOOL_RESPONSE, "model": "claude-haiku-5-5"})

    p = AnthropicProvider(None, client=anthropic_client(handler))
    await p.generate("s", [Message(role="user", text="x")], [], ModelConfig(model="claude-haiku-5-5"))
    assert "fallbacks" not in seen["body"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, E.ProviderRateLimitError),
        (500, E.ProviderUnavailableError),
        (529, E.ProviderUnavailableError),
        (401, E.ProviderAuthError),
        (400, E.ProviderRequestError),
        (404, E.ProviderRequestError),
    ],
)
async def test_anthropic_error_mapping(status, expected):
    def handler(request):
        return httpx2.Response(status, json={"type": "error", "error": {"type": "x", "message": "boom"}})

    p = AnthropicProvider(None, client=anthropic_client(handler))
    with pytest.raises(expected) as info:
        await p.generate("s", [Message(role="user", text="x")], [], ModelConfig(model="claude-haiku-5-5"))
    assert info.value.retryable == issubclass(expected, E.TransientError)


async def test_anthropic_refusal_is_normalized():
    def handler(request):
        return httpx2.Response(
            200,
            json={
                **ANTHROPIC_TOOL_RESPONSE,
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "category": "cyber", "explanation": "no"},
            },
        )

    p = AnthropicProvider(None, client=anthropic_client(handler))
    resp = await p.generate("s", [Message(role="user", text="x")], [], ModelConfig(model="claude-haiku-5-5"))
    assert resp.finish_reason == "refusal" and resp.error["stop_details"]["category"] == "cyber"


def test_missing_keys_raise_not_configured():
    with pytest.raises(E.ProviderNotConfiguredError):
        AnthropicProvider(None)
    with pytest.raises(E.ProviderNotConfiguredError):
        OpenAIProvider(None)


OPENAI_RESPONSE = {
    "id": "resp_1",
    "object": "response",
    "created_at": 1,
    "status": "completed",
    "model": "test-model",
    "output": [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc"},
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "Computing.", "annotations": []}],
        },
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "calculator",
            "arguments": '{"operation": "max"}',
            "status": "completed",
        },
    ],
    "usage": {
        "input_tokens": 80,
        "output_tokens": 12,
        "total_tokens": 92,
        "input_tokens_details": {"cached_tokens": 10},
        "output_tokens_details": {"reasoning_tokens": 4},
    },
    "parallel_tool_calls": True,
    "tool_choice": "auto",
    "tools": [],
}


async def test_openai_request_and_normalization():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=OPENAI_RESPONSE)

    p = OpenAIProvider(None, client=openai_client(handler))
    history = [
        Message(role="user", text="hi"),
        Message(
            role="assistant",
            provider="openai",
            raw=OPENAI_RESPONSE["output"],
            tool_calls=[ToolCall("call_0", "calculator", {})],
        ),
        Message(role="tool_results", tool_results=[ToolResult("call_0", "calculator", '{"result": 2}')]),
    ]
    resp = await p.generate("sys", history, TOOLS, ModelConfig(model="test-model", effort="low"))
    body = seen["body"]
    assert seen["url"].endswith("/responses")
    assert body["instructions"] == "sys" and body["store"] is False
    assert body["include"] == ["reasoning.encrypted_content"] and body["reasoning"] == {"effort": "low"}
    assert body["tools"][0] == {
        "type": "function",
        "name": "calculator",
        "description": "math",
        "parameters": TOOLS[0].input_schema,
        "strict": False,
    }
    items = body["input"]
    assert items[0] == {"role": "user", "content": "hi"}
    assert items[1]["type"] == "reasoning" and items[1]["encrypted_content"] == "enc"
    assert items[2] == {"role": "assistant", "content": "Computing."}
    assert items[3] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "calculator",
        "arguments": '{"operation": "max"}',
    }
    assert items[4] == {"type": "function_call_output", "call_id": "call_0", "output": '{"result": 2}'}
    assert resp.text == "Computing." and resp.finish_reason == "tool_use"
    assert resp.tool_calls[0].id == "call_1" and resp.tool_calls[0].arguments == {"operation": "max"}
    assert resp.usage.cached_input_tokens == 10 and resp.usage.cache_write_tokens is None


async def test_openai_requires_model_and_maps_errors():
    p = OpenAIProvider(
        None, client=openai_client(lambda r: httpx2.Response(429, json={"error": {"message": "slow"}}))
    )
    with pytest.raises(E.ProviderNotConfiguredError):
        await p.generate("s", [], [], ModelConfig(model=""))
    with pytest.raises(E.ProviderRateLimitError):
        await p.generate("s", [Message(role="user", text="x")], [], ModelConfig(model="m"))


async def test_mock_is_deterministic_and_simulated():
    p = MockProvider()
    msgs = [Message(role="user", text='<task_input>{"input": {"platform": "Nimbus Inference"}}</task_input>')]
    cfg = ModelConfig(model="mock-research", extra={"scenario": "research"})
    a, b = await p.generate("s", msgs, [], cfg), await p.generate("s", msgs, [], cfg)
    assert (
        a.tool_calls[0].name == "document_search" and a.tool_calls[0].arguments == b.tool_calls[0].arguments
    )
    assert a.usage.simulated and a.usage.input_tokens == b.usage.input_tokens


async def test_mock_failure_scenarios_depend_on_attempt():
    p = MockProvider()
    msgs = [Message(role="user", text="x")]
    with pytest.raises(E.ProviderRateLimitError):
        await p.generate(
            "s",
            msgs,
            [],
            ModelConfig(
                model="mock-rate_limit", extra={"scenario": "rate_limit"}, metadata={"attempt_number": 1}
            ),
        )
    ok = await p.generate(
        "s",
        msgs,
        [],
        ModelConfig(
            model="mock-rate_limit", extra={"scenario": "rate_limit"}, metadata={"attempt_number": 3}
        ),
    )
    assert ok.finish_reason == "end_turn"
    with pytest.raises(E.ProviderTimeoutError):
        await p.generate("s", msgs, [], ModelConfig(model="mock-timeout", extra={"scenario": "timeout"}))
