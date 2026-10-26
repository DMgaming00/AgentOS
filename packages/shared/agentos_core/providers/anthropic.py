"""Anthropic Claude adapter (official ``anthropic`` SDK, Messages API)."""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

from agentos_core.domain.errors import (
    ProviderAuthError,
    ProviderNotConfiguredError,
    ProviderRateLimitError,
    ProviderRequestError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from agentos_core.providers.base import (
    FinishReason,
    Message,
    ModelConfig,
    ModelResponse,
    ToolCall,
    ToolDefinition,
    Usage,
)
from agentos_core.providers.execution import GuardedTransport, check_provider_call, client_is_networkless

# Models that accept the server-side refusal fallback (``fallbacks: "default"``).
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_MAP: dict[str, FinishReason] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "stop_sequence": "end_turn",
    "pause_turn": "other",
}


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            out.append({"role": "user", "content": m.text or ""})
        elif m.role == "assistant":
            if m.provider == "anthropic" and m.raw:
                # Replay the original content blocks unchanged (keeps thinking blocks valid).
                out.append({"role": "assistant", "content": m.raw})
            else:
                blocks: list[dict[str, Any]] = []
                if m.text:
                    blocks.append({"type": "text", "text": m.text})
                blocks += [
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                    for c in m.tool_calls
                ]
                out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
        else:
            # All results of one assistant turn go back in a single user message.
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": r.call_id,
                            "content": r.content,
                            "is_error": r.is_error,
                        }
                        for r in m.tool_results
                    ],
                }
            )
    return out


def map_error(exc: Exception) -> Exception:
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderRateLimitError(f"anthropic rate limited: {exc.message}")
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeoutError("anthropic request timed out")
    if isinstance(exc, anthropic.APIConnectionError):
        return ProviderUnavailableError(f"anthropic connection error: {exc}")
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderAuthError(f"anthropic auth error: {exc.message}")
    if isinstance(
        exc, anthropic.BadRequestError | anthropic.NotFoundError | anthropic.UnprocessableEntityError
    ):
        return ProviderRequestError(f"anthropic rejected the request: {exc.message}")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409, 529):
            return ProviderUnavailableError(f"anthropic error {exc.status_code}: {exc.message}")
        return ProviderRequestError(f"anthropic error {exc.status_code}: {exc.message}")
    return exc


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None,
        *,
        client: anthropic.AsyncAnthropic | None = None,
        refusal_fallback: bool = True,
    ) -> None:
        if client is None and not api_key:
            raise ProviderNotConfiguredError(
                "ANTHROPIC_API_KEY is not set; use the mock provider or configure a key"
            )
        # SDK retries are disabled: the engine retries with backoff so attempts are visible and fenced.
        self.client = client or anthropic.AsyncAnthropic(
            api_key=api_key,
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=GuardedTransport("anthropic")),
        )
        self.refusal_fallback = refusal_fallback

    async def generate(
        self, system: str, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> ModelResponse:
        check_provider_call("anthropic", config.model, network_capable=not client_is_networkless(self.client))
        params: dict[str, Any] = {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "system": system,
            "messages": to_anthropic_messages(messages),
        }
        if tools:
            params["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
            ]
        if config.effort:
            params["output_config"] = {"effort": config.effort}
        use_fallback = self.refusal_fallback and config.model in FALLBACK_MODELS
        started = time.perf_counter()
        try:
            client = self.client.with_options(timeout=config.timeout_seconds)
            if use_fallback:
                resp: Any = await client.beta.messages.create(
                    betas=[FALLBACK_BETA], fallbacks="default", **params
                )
            else:
                resp = await client.messages.create(**params)
        except anthropic.AnthropicError as exc:
            raise map_error(exc) from exc
        latency = int((time.perf_counter() - started) * 1000)
        return parse_response(resp, latency)


def parse_response(resp: Any, latency_ms: int) -> ModelResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    raw: list[dict[str, Any]] = []
    for block in resp.content:
        raw.append(block.model_dump(mode="json", exclude_none=True))
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            args = block.input if isinstance(block.input, dict) else json.loads(block.input)
            calls.append(ToolCall(id=block.id, name=block.name, arguments=args))
    u = resp.usage
    usage = Usage(
        input_tokens=getattr(u, "input_tokens", None),
        output_tokens=getattr(u, "output_tokens", None),
        cached_input_tokens=getattr(u, "cache_read_input_tokens", None),
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None),
    )
    return ModelResponse(
        text="".join(text_parts),
        tool_calls=calls,
        model=resp.model,
        usage=usage,
        latency_ms=latency_ms,
        finish_reason=_STOP_MAP.get(resp.stop_reason or "", "other"),
        provider="anthropic",
        raw=raw,
        request_id=getattr(resp, "_request_id", None),
        error=(
            {"stop_details": resp.stop_details.model_dump(mode="json")}
            if resp.stop_reason == "refusal" and getattr(resp, "stop_details", None)
            else None
        ),
    )
