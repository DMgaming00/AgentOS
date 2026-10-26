"""Budgeted, observable model calls.

Before each call the run budget is reserved atomically in PostgreSQL; afterwards
the reservation is reconciled against the reported usage. Usage the provider did
not report is stored as NULL and the cost basis is marked ``partial``/``unknown``.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from opentelemetry import trace

from agentos_core.config import get_settings
from agentos_core.domain.errors import AgentOSError, classify
from agentos_core.engine.core import UsageRecord, emit_event, record_model_call, reserve_model_call
from agentos_core.flight.capture import capture
from agentos_core.pricing import get_pricing
from agentos_core.providers.base import Message, ModelConfig, ModelResponse, ToolDefinition
from agentos_core.providers.execution import enforce_real_spend
from agentos_core.providers.factory import get_provider
from agentos_core.redaction import redact_text

tracer = trace.get_tracer("agentos.models")


async def call_model(
    *,
    provider_name: str,
    system: str,
    messages: list[Message],
    tools: list[ToolDefinition],
    config: ModelConfig,
    run_id: uuid.UUID,
    step_id: uuid.UUID | None,
    attempt_id: uuid.UUID | None,
    agent_definition_id: uuid.UUID | None,
    node_key: str,
    reservation_usd: Decimal | None = None,
    agent_version_id: uuid.UUID | None = None,
    attempt_number: int | None = None,
    meta: dict[str, Any] | None = None,
) -> ModelResponse:
    reserve = reservation_usd or Decimal(str(get_settings().default_model_call_reservation_usd))
    record_id = uuid.uuid4()  # the Flight Recorder row for this call; the events below reference it
    meta = {**(meta or {}), "provider": provider_name, "node_key": node_key}
    await reserve_model_call(run_id, reserve)
    await emit_event(
        run_id,
        "model.call.started",
        {
            "node_key": node_key,
            "provider": provider_name,
            "model": config.model,
            "tools": [t.name for t in tools],
        },
        step_id=step_id,
    )
    provider = get_provider(provider_name)
    with tracer.start_as_current_span(
        "model.call",
        attributes={
            "agentos.run_id": str(run_id),
            "agentos.node_key": node_key,
            "gen_ai.system": provider_name,
            "gen_ai.request.model": config.model,
        },
    ) as span:
        try:
            await enforce_real_spend(provider_name, reserve)
            resp = await provider.generate(system, messages, tools, config)
        except Exception as exc:
            err: AgentOSError = classify(exc)
            span.record_exception(exc)
            span.set_status(trace.Status(trace.StatusCode.ERROR, err.error_type))
            await record_model_call(
                run_id=run_id,
                step_id=step_id,
                attempt_id=attempt_id,
                agent_definition_id=agent_definition_id,
                usage=UsageRecord(
                    provider=provider_name,
                    model=config.model,
                    input_tokens=None,
                    output_tokens=None,
                    cached_input_tokens=None,
                    cache_write_tokens=None,
                    latency_ms=0,
                    estimated_cost_usd=None,
                    cost_basis="unknown",
                    pricing_version=get_pricing().version,
                    finish_reason=None,
                    status="failed",
                    error_type=err.error_type,
                ),
                reserved_usd=reserve,
                event_payload={
                    "node_key": node_key,
                    "provider": provider_name,
                    "model": config.model,
                    "error": err.to_dict(),
                    "record_id": str(record_id),
                },
                body=capture(record_id, system, messages, tools, config, meta=meta, error=err.to_dict()),
                node_key=node_key,
                attempt_number=attempt_number,
                agent_version_id=agent_version_id,
            )
            raise err from exc
        u = resp.usage
        estimate = get_pricing().estimate(
            provider_name,
            resp.model,
            input_tokens=u.input_tokens,
            output_tokens=u.output_tokens,
            cached_input_tokens=u.cached_input_tokens,
            cache_write_tokens=u.cache_write_tokens,
        )
        span.set_attribute("gen_ai.response.model", resp.model)
        span.set_attribute("gen_ai.response.finish_reason", resp.finish_reason)
        if u.input_tokens is not None:
            span.set_attribute("gen_ai.usage.input_tokens", u.input_tokens)
        if u.output_tokens is not None:
            span.set_attribute("gen_ai.usage.output_tokens", u.output_tokens)
        payload: dict[str, Any] = {
            "node_key": node_key,
            "provider": provider_name,
            "model": resp.model,
            "finish_reason": resp.finish_reason,
            "latency_ms": resp.latency_ms,
            "input_tokens": u.input_tokens,
            "output_tokens": u.output_tokens,
            "cached_input_tokens": u.cached_input_tokens,
            "estimated_cost_usd": str(estimate.usd) if estimate.usd is not None else None,
            "cost_basis": estimate.basis,
            "simulated": u.simulated,
            "tool_calls": [{"id": c.id, "name": c.name} for c in resp.tool_calls],
            "text_preview": redact_text(resp.text[:2000]) if resp.text else None,
            "request_id": resp.request_id,
            "record_id": str(record_id),
        }
        if isinstance(resp.raw, dict) and "replay" in resp.raw:
            payload["replay"] = resp.raw["replay"]
        await record_model_call(
            run_id=run_id,
            step_id=step_id,
            attempt_id=attempt_id,
            agent_definition_id=agent_definition_id,
            usage=UsageRecord(
                provider=provider_name,
                model=resp.model,
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                cached_input_tokens=u.cached_input_tokens,
                cache_write_tokens=u.cache_write_tokens,
                latency_ms=resp.latency_ms,
                estimated_cost_usd=estimate.usd,
                cost_basis=estimate.basis,
                pricing_version=estimate.pricing_version,
                finish_reason=resp.finish_reason,
            ),
            reserved_usd=reserve,
            event_payload=payload,
            body=capture(record_id, system, messages, tools, config, meta=meta, response=resp),
            node_key=node_key,
            attempt_number=attempt_number,
            agent_version_id=agent_version_id,
        )
        return resp
