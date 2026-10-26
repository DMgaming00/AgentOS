"""Sanitized capture of model requests and responses for the Flight Recorder.

What is stored: system prompt, messages, tool names/descriptions, model configuration, the response text,
tool calls with arguments, finish reason, usage. What is never stored: provider-raw payloads (thinking blocks,
signatures), API keys, anything matching the secret patterns in ``agentos_core.redaction``. Oversize requests
keep the first message and the most recent ones and say how many were omitted.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agentos_core.providers.base import Message, ModelConfig, ModelResponse, ToolDefinition
from agentos_core.providers.fixtures import request_digest
from agentos_core.redaction import redact, redact_text

MAX_TEXT_CHARS = 20_000
MAX_REQUEST_BYTES = 200_000
KEEP_RECENT_MESSAGES = 8


@dataclass
class CallBody:
    record_id: Any
    request: dict[str, Any]
    response: dict[str, Any] | None
    error: dict[str, Any] | None
    request_digest: str
    truncated: bool
    meta: dict[str, Any]


def _text(value: str | None, limit: int = MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    clean = redact_text(value)
    return clean if len(clean) <= limit else clean[:limit] + f"…[+{len(clean) - limit} chars truncated]"


def sanitize_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        d: dict[str, Any] = {"role": m.role}
        if m.text:
            d["text"] = _text(m.text)
        if m.tool_calls:
            d["tool_calls"] = [
                {"id": c.id, "name": c.name, "arguments": redact(c.arguments)} for c in m.tool_calls
            ]
        if m.tool_results:
            d["tool_results"] = [
                {"call_id": r.call_id, "name": r.name, "content": _text(r.content), "is_error": r.is_error}
                for r in m.tool_results
            ]
        out.append(d)  # m.raw (provider thinking blocks, signatures) is deliberately dropped
    return out


def build_request(
    system: str,
    messages: list[Message],
    tools: list[ToolDefinition],
    config: ModelConfig,
    *,
    meta: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    body: dict[str, Any] = {
        "system": _text(system, MAX_TEXT_CHARS * 2),
        "messages": sanitize_messages(messages),
        "tools": [
            {
                "name": t.name,
                "description": _text(t.description, 500),
                "schema_sha256": hashlib.sha256(
                    json.dumps(t.input_schema, sort_keys=True).encode()
                ).hexdigest()[:16],
            }
            for t in tools
        ],
        "config": {
            "model": config.model,
            "max_tokens": config.max_tokens,
            "effort": config.effort,
            "timeout_seconds": config.timeout_seconds,
        },
        "meta": meta,
    }
    truncated = False
    if (
        len(json.dumps(body, default=str)) > MAX_REQUEST_BYTES
        and len(body["messages"]) > KEEP_RECENT_MESSAGES + 1
    ):
        msgs = body["messages"]
        omitted = len(msgs) - 1 - KEEP_RECENT_MESSAGES
        body["messages"] = [msgs[0], {"role": "omitted", "count": omitted}, *msgs[-KEEP_RECENT_MESSAGES:]]
        truncated = True
    return body, truncated


def build_response(resp: ModelResponse) -> dict[str, Any]:
    u = resp.usage
    return {
        "text": _text(resp.text),
        "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": redact(c.arguments)} for c in resp.tool_calls
        ],
        "finish_reason": resp.finish_reason,
        "model": resp.model,
        "request_id": resp.request_id,
        "usage": {
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cached_input_tokens": u.cached_input_tokens,
            "simulated": u.simulated,
        },
    }


def capture(
    record_id: Any,
    system: str,
    messages: list[Message],
    tools: list[ToolDefinition],
    config: ModelConfig,
    *,
    meta: dict[str, Any],
    response: ModelResponse | None = None,
    error: dict[str, Any] | None = None,
) -> CallBody:
    request, truncated = build_request(system, messages, tools, config, meta=meta)
    return CallBody(
        record_id=record_id,
        request=request,
        response=build_response(response) if response else None,
        error=redact(error) if error else None,
        # The digest is computed on the unsanitized request so replay can detect divergence exactly.
        request_digest=request_digest(system, messages, tools, config.model),
        truncated=truncated,
        meta=meta,
    )
