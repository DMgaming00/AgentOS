"""Stored, versioned model-response fixtures: a network-free provider for offline development.

A fixture set is a JSON file ``<name>.v<N>.json``::

    {"fixture_set": "...", "version": 1, "simulated": true,
     "fixtures": [{"id": "...", "match": {...}, "response": {...}}]}

``match`` is either ``{"request_digest": "<sha256>"}`` (exact request) or
``{"system_contains": str, "last_user_contains": str}`` (substring, both optional). A request can also pick
a fixture explicitly with ``ModelConfig.extra["fixture_id"]``. When nothing matches, the provider raises
``FixtureMissError``: it never falls back to canned text, so a missing fixture is a visible test failure.
Every response is labeled simulated and carries the fixture set and version in its model string.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agentos_core.domain.errors import PermanentError, ProviderNotConfiguredError
from agentos_core.providers.base import Message, ModelConfig, ModelResponse, ToolCall, ToolDefinition, Usage
from agentos_core.redaction import redact, redact_text

FIXTURE_DIR = Path(__file__).resolve().parents[4] / "fixtures" / "model_responses"


class FixtureMissError(PermanentError):
    error_type = "fixture_missing"


def request_digest(system: str, messages: list[Message], tools: list[ToolDefinition], model: str) -> str:
    """Stable identity of a request: raw provider payloads and call ids are excluded."""
    canon = {
        "model": model,
        "system": system,
        "messages": [
            {
                "role": m.role,
                "text": m.text,
                "tool_calls": [{"name": c.name, "arguments": c.arguments} for c in m.tool_calls],
                "tool_results": [
                    {"name": r.name, "content": r.content, "is_error": r.is_error} for r in m.tool_results
                ],
            }
            for m in messages
        ],
        "tools": sorted(t.name for t in tools),
    }
    return hashlib.sha256(json.dumps(canon, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FixtureSet:
    def __init__(self, data: dict[str, Any], *, source: str = "") -> None:
        for key in ("fixture_set", "version", "fixtures"):
            if key not in data:
                raise ProviderNotConfiguredError(f"fixture file {source or '?'} is missing {key!r}")
        if data.get("simulated") is not True:
            raise ProviderNotConfiguredError(
                f"fixture set {data['fixture_set']!r} must declare simulated: true"
            )
        self.name: str = data["fixture_set"]
        self.version: int = int(data["version"])
        self.fixtures: list[dict[str, Any]] = data["fixtures"]
        ids = [f["id"] for f in self.fixtures]
        if len(ids) != len(set(ids)):
            raise ProviderNotConfiguredError(f"fixture set {self.name!r} has duplicate ids")
        self.digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]

    @classmethod
    def load(cls, path: Path) -> FixtureSet:
        return cls(json.loads(path.read_text()), source=str(path))

    def find(
        self, digest: str, system: str, messages: list[Message], fixture_id: str | None
    ) -> dict[str, Any] | None:
        if fixture_id:
            return next((f for f in self.fixtures if f["id"] == fixture_id), None)
        for f in self.fixtures:
            if f["match"].get("request_digest") == digest:
                return f
        last_user = next((m.text or "" for m in reversed(messages) if m.role == "user"), "")
        for f in self.fixtures:
            m = f["match"]
            if "request_digest" in m or not (m.get("system_contains") or m.get("last_user_contains")):
                continue
            if m.get("system_contains", "") in system and m.get("last_user_contains", "") in last_user:
                return f
        return None


def load_fixture_sets(directory: Path = FIXTURE_DIR) -> list[FixtureSet]:
    return [FixtureSet.load(p) for p in sorted(directory.glob("*.v*.json"))]


class FixtureProvider:
    name = "fixture"

    def __init__(self, sets: list[FixtureSet] | None = None) -> None:
        self.sets = load_fixture_sets() if sets is None else sets

    async def generate(
        self, system: str, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> ModelResponse:
        digest = request_digest(system, messages, tools, config.model)
        fixture_id = config.extra.get("fixture_id")
        for fs in self.sets:
            hit = fs.find(digest, system, messages, fixture_id)
            if hit is None:
                continue
            r = hit["response"]
            u = r.get("usage") or {}
            return ModelResponse(
                text=r.get("text", ""),
                tool_calls=[
                    ToolCall(id=f"fixture_{hit['id']}_{i}", name=c["name"], arguments=c.get("arguments", {}))
                    for i, c in enumerate(r.get("tool_calls") or [])
                ],
                model=f"fixture:{fs.name}@v{fs.version}",
                usage=Usage(
                    input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"), simulated=True
                ),
                latency_ms=int(r.get("latency_ms", 1)),
                finish_reason=r.get("finish_reason", "tool_use" if r.get("tool_calls") else "end_turn"),
                provider="fixture",
                request_id=f"{fs.name}@v{fs.version}:{hit['id']}",
            )
        raise FixtureMissError(
            f"no stored fixture matches this request (digest {digest[:16]}, fixture_id={fixture_id!r}); "
            "record one with fixture_entry() or fix the match rule",
            details={"request_digest": digest},
        )


def fixture_entry(
    fixture_id: str,
    system: str,
    messages: list[Message],
    tools: list[ToolDefinition],
    config: ModelConfig,
    response: ModelResponse,
) -> dict[str, Any]:
    """Turn a response into a sanitized fixture entry keyed by the exact request digest (for recording later)."""
    return {
        "id": fixture_id,
        "match": {"request_digest": request_digest(system, messages, tools, config.model)},
        "response": {
            "text": redact_text(response.text),
            "tool_calls": [{"name": c.name, "arguments": redact(c.arguments)} for c in response.tool_calls],
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
            "finish_reason": response.finish_reason,
            "latency_ms": response.latency_ms,
        },
    }
