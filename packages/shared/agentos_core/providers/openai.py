"""OpenAI adapter (official ``openai`` SDK, Responses API).

Requests use ``store=False`` so conversation state is held by AgentOS (and
checkpointed in PostgreSQL), not on the provider side. For reasoning models the
encrypted reasoning items are requested and replayed unchanged.
"""

from __future__ import annotations

import json
import time
from typing import Any

import openai

from agentos_core.domain.errors import (
    InvalidModelOutputError,
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


def to_openai_input(messages: list[Message]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "user":
            items.append({"role": "user", "content": m.text or ""})
        elif m.role == "assistant":
            if m.provider == "openai" and m.raw:
                items.extend(_replay_item(i) for i in m.raw)
            else:
                if m.text:
                    items.append({"role": "assistant", "content": m.text})
                items += [
                    {
                        "type": "function_call",
                        "call_id": c.id,
                        "name": c.name,
                        "arguments": json.dumps(c.arguments),
                    }
                    for c in m.tool_calls
                ]
        else:
            items += [
                {"type": "function_call_output", "call_id": r.call_id, "output": r.content}
                for r in m.tool_results
            ]
    return items


def _replay_item(item: dict[str, Any]) -> dict[str, Any]:
    """Convert an output item into the minimal input item for stateless replay."""
    kind = item.get("type")
    if kind == "message":
        text = "".join(p.get("text", "") for p in item.get("content", []) if p.get("type") == "output_text")
        return {"role": "assistant", "content": text}
    if kind == "function_call":
        return {k: item[k] for k in ("type", "call_id", "name", "arguments") if k in item}
    return item  # reasoning items carry encrypted_content and are replayed as returned


def map_error(exc: Exception) -> Exception:
    if isinstance(exc, openai.RateLimitError):
        return ProviderRateLimitError(f"openai rate limited: {exc.message}")
    if isinstance(exc, openai.APITimeoutError):
        return ProviderTimeoutError("openai request timed out")
    if isinstance(exc, openai.APIConnectionError):
        return ProviderUnavailableError(f"openai connection error: {exc}")
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderAuthError(f"openai auth error: {exc.message}")
    if isinstance(exc, openai.BadRequestError | openai.NotFoundError | openai.UnprocessableEntityError):
        return ProviderRequestError(f"openai rejected the request: {exc.message}")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409):
            return ProviderUnavailableError(f"openai error {exc.status_code}: {exc.message}")
        return ProviderRequestError(f"openai error {exc.status_code}: {exc.message}")
    return exc


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None, *, client: openai.AsyncOpenAI | None = None) -> None:
        if client is None and not api_key:
            raise ProviderNotConfiguredError(
                "OPENAI_API_KEY is not set; use the mock provider or configure a key"
            )
        self.client = client or openai.AsyncOpenAI(
            api_key=api_key,
            max_retries=0,
            http_client=openai.DefaultAsyncHttpxClient(transport=GuardedTransport("openai")),
        )

    async def generate(
        self, system: str, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> ModelResponse:
        check_provider_call("openai", config.model, network_capable=not client_is_networkless(self.client))
        if not config.model:
            raise ProviderNotConfiguredError(
                "no OpenAI model configured (set AGENTOS_OPENAI_MODEL or the agent model)"
            )
        params: dict[str, Any] = {
            "model": config.model,
            "instructions": system,
            "input": to_openai_input(messages),
            "max_output_tokens": config.max_tokens,
            "store": False,
        }
        if tools:
            params["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    "strict": False,
                }
                for t in tools
            ]
        if config.extra.get("reasoning", True):
            params["include"] = ["reasoning.encrypted_content"]
            if config.effort:
                params["reasoning"] = {"effort": config.effort}
        started = time.perf_counter()
        try:
            resp = await self.client.with_options(timeout=config.timeout_seconds).responses.create(**params)
        except openai.OpenAIError as exc:
            raise map_error(exc) from exc
        return parse_response(resp, int((time.perf_counter() - started) * 1000))


def parse_response(resp: Any, latency_ms: int) -> ModelResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    raw: list[dict[str, Any]] = []
    refused = False
    for item in resp.output or []:
        dumped = item.model_dump(mode="json", exclude_none=True)
        raw.append(dumped)
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    text_parts.append(part.text)
                elif part.type == "refusal":
                    refused = True
                    text_parts.append(part.refusal)
        elif item.type == "function_call":
            try:
                args = json.loads(item.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise InvalidModelOutputError(f"tool call arguments are not valid JSON: {exc}") from exc
            calls.append(ToolCall(id=item.call_id, name=item.name, arguments=args))
    finish: FinishReason = "tool_use" if calls else "end_turn"
    if refused:
        finish = "refusal"
    elif resp.status == "incomplete":
        reason = getattr(resp.incomplete_details, "reason", None)
        finish = "max_tokens" if reason == "max_output_tokens" else "other"
    u = resp.usage
    usage = Usage(
        input_tokens=getattr(u, "input_tokens", None) if u else None,
        output_tokens=getattr(u, "output_tokens", None) if u else None,
        cached_input_tokens=getattr(getattr(u, "input_tokens_details", None), "cached_tokens", None)
        if u
        else None,
    )
    return ModelResponse(
        text="".join(text_parts),
        tool_calls=calls,
        model=resp.model,
        usage=usage,
        latency_ms=latency_ms,
        finish_reason=finish,
        provider="openai",
        raw=raw,
        request_id=getattr(resp, "_request_id", None),
    )
