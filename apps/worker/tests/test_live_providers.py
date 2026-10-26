"""OPT-IN smoke tests against real provider APIs (cost real money; never run in default CI).

(Also requires the four execution flags: see docs/ENABLE_REAL_CLAUDE.md; offline mode blocks these tests by design.)

AGENTOS_LIVE_TESTS=1 ANTHROPIC_API_KEY=... uv run pytest apps/worker/tests/test_live_providers.py -m live
AGENTOS_LIVE_TESTS=1 OPENAI_API_KEY=... AGENTOS_OPENAI_MODEL=<model> uv run pytest ... -m live
"""

import os

import pytest

from agentos_core.providers.base import Message, ModelConfig, ToolDefinition, ToolResult

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("AGENTOS_LIVE_TESTS") != "1",
        reason="live provider tests are opt-in (AGENTOS_LIVE_TESTS=1)",
    ),
]

CALC = ToolDefinition(
    "calculator",
    "Add numbers. Always use this tool for arithmetic.",
    {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["add"]},
            "values": {"type": "array", "items": {"type": "number"}},
        },
        "required": ["operation", "values"],
    },
)


async def _tool_round_trip(provider, model: str) -> None:
    cfg = ModelConfig(model=model, max_tokens=2000, timeout_seconds=120)
    msgs = [
        Message(
            role="user",
            text="What is 17.5 + 24.5? Use the calculator tool, then answer with just the number.",
        )
    ]
    first = await provider.generate("You are a precise assistant.", msgs, [CALC], cfg)
    assert first.tool_calls, f"expected a tool call, got finish={first.finish_reason} text={first.text!r}"
    call = first.tool_calls[0]
    assert call.name == "calculator" and sorted(call.arguments["values"]) == [17.5, 24.5]
    assert first.usage.input_tokens and first.usage.output_tokens
    msgs += [
        first.as_message(),
        Message(role="tool_results", tool_results=[ToolResult(call.id, "calculator", '{"result": 42}')]),
    ]
    second = await provider.generate("You are a precise assistant.", msgs, [CALC], cfg)
    assert "42" in second.text and second.finish_reason == "end_turn"


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")
async def test_anthropic_live_tool_round_trip():
    from agentos_core.config import get_settings
    from agentos_core.providers.anthropic import AnthropicProvider

    await _tool_round_trip(AnthropicProvider(os.environ["ANTHROPIC_API_KEY"]), get_settings().anthropic_model)


@pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") and os.environ.get("AGENTOS_OPENAI_MODEL")),
    reason="OPENAI_API_KEY and AGENTOS_OPENAI_MODEL not set",
)
async def test_openai_live_tool_round_trip():
    from agentos_core.providers.openai import OpenAIProvider

    await _tool_round_trip(OpenAIProvider(os.environ["OPENAI_API_KEY"]), os.environ["AGENTOS_OPENAI_MODEL"])
