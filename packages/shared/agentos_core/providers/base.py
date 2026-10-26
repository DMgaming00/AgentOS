"""Provider-neutral model interface.

Conversations are stored in a normalized, JSON-serializable form so they can be
checkpointed in PostgreSQL and resumed by any worker. Assistant turns keep the
provider's raw output (``raw``) so provider-specific content such as thinking
or reasoning items is replayed unchanged on the next request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

FinishReason = Literal["end_turn", "tool_use", "max_tokens", "refusal", "other"]


@dataclass
class ToolDefinition:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: Literal["user", "assistant", "tool_results"]
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    provider: str | None = None
    raw: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "text": self.text,
            "tool_calls": [c.__dict__ for c in self.tool_calls],
            "tool_results": [r.__dict__ for r in self.tool_results],
            "provider": self.provider,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Message:
        return cls(
            role=d["role"],
            text=d.get("text"),
            tool_calls=[ToolCall(**c) for c in d.get("tool_calls") or []],
            tool_results=[ToolResult(**r) for r in d.get("tool_results") or []],
            provider=d.get("provider"),
            raw=d.get("raw"),
        )


@dataclass
class ModelConfig:
    model: str
    max_tokens: int = 16000
    effort: str | None = None
    timeout_seconds: float = 120.0
    extra: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Usage:
    """Token usage. ``None`` means the provider did not report the value."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    cache_write_tokens: int | None = None
    simulated: bool = False


@dataclass
class ModelResponse:
    text: str
    tool_calls: list[ToolCall]
    model: str
    usage: Usage
    latency_ms: int
    finish_reason: FinishReason
    provider: str
    raw: Any = None
    request_id: str | None = None
    error: dict[str, Any] | None = None

    def as_message(self) -> Message:
        return Message(
            role="assistant",
            text=self.text or None,
            tool_calls=self.tool_calls,
            provider=self.provider,
            raw=self.raw,
        )


class ModelProvider(Protocol):
    name: str

    async def generate(
        self, system: str, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> ModelResponse: ...
