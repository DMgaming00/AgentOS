"""Replays model responses recorded by the Flight Recorder (deterministic re-execution).

Used only for runs with ``purpose='replay'``. The provider never touches the network: the i-th model call of a
step returns the i-th successful recorded response of the same node in the source run. If the re-execution asks
for more calls than were recorded, that is a *divergence* and fails loudly; if a call's request differs from the
recorded one (different tool results, different prompt) the response is still returned but flagged, so the
comparison shows exactly where behavior changed.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from agentos_core.db.models import ModelCallRecord
from agentos_core.db.session import get_sessionmaker
from agentos_core.domain.errors import PermanentError
from agentos_core.providers.base import Message, ModelConfig, ModelResponse, ToolCall, ToolDefinition, Usage
from agentos_core.providers.fixtures import request_digest


class ReplayDivergenceError(PermanentError):
    error_type = "replay_diverged"


class RecordedProvider:
    name = "recorded"

    async def generate(
        self, system: str, messages: list[Message], tools: list[ToolDefinition], config: ModelConfig
    ) -> ModelResponse:
        source = uuid.UUID(str(config.extra["replay_run_id"]))
        node_key = str(config.extra["replay_node_key"])
        index = sum(1 for m in messages if m.role == "assistant")
        async with get_sessionmaker()() as s:
            rows = list(
                (
                    await s.execute(
                        select(ModelCallRecord)
                        .where(
                            ModelCallRecord.run_id == source,
                            ModelCallRecord.node_key == node_key,
                            ModelCallRecord.status == "succeeded",
                        )
                        .order_by(ModelCallRecord.created_at, ModelCallRecord.call_index)
                    )
                ).scalars()
            )
        if index >= len(rows):
            raise ReplayDivergenceError(
                f"re-execution of {node_key!r} asked for model call #{index + 1} but only {len(rows)} were recorded",
                details={"node_key": node_key, "recorded_calls": len(rows), "requested_call": index + 1},
            )
        rec = rows[index]
        r: dict[str, Any] = rec.response or {}
        u = r.get("usage") or {}
        matched = (
            request_digest(system, messages, tools, rec.request["config"]["model"]) == rec.request_digest
        )
        return ModelResponse(
            text=r.get("text") or "",
            tool_calls=[
                ToolCall(id=c["id"], name=c["name"], arguments=c.get("arguments") or {})
                for c in r.get("tool_calls", [])
            ],
            model=f"recorded:{rec.model}",
            usage=Usage(
                input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens"), simulated=True
            ),
            latency_ms=1,
            finish_reason=r.get("finish_reason", "end_turn"),
            provider="recorded",
            request_id=f"recorded:{rec.id}",
            raw={"replay": {"record_id": str(rec.id), "request_matches_recording": matched}},
        )
