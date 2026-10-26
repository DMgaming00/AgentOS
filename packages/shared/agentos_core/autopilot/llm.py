"""Model calls made by Autopilot itself (diagnosis, optimizer).

These are not part of a workflow run, so they are budgeted per optimization cycle
(``autopilot_ref`` = the diagnosis id) against ``policy.max_cycle_spend_usd`` and recorded
in ``model_usage`` with ``purpose``. A failing real provider raises; it is never replaced
by the mock.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select

from agentos_core.config import get_settings
from agentos_core.db.models import ModelUsage
from agentos_core.db.session import get_sessionmaker
from agentos_core.domain.errors import BudgetExceededError, ProviderNotConfiguredError, classify
from agentos_core.engine.tx import unit_of_work
from agentos_core.pricing import get_pricing
from agentos_core.providers.base import Message, ModelConfig, ModelResponse
from agentos_core.providers.execution import enforce_real_spend
from agentos_core.providers.factory import get_provider
from agentos_core.util import utcnow

RESERVATION_USD = Decimal("0.05")


def autopilot_model(provider: str, scenario: str) -> ModelConfig:
    s = get_settings()
    if provider == "mock":
        return ModelConfig(model=f"mock-{scenario}", max_tokens=8000, extra={"scenario": scenario})
    if provider == "anthropic":
        return ModelConfig(
            model=s.autopilot_anthropic_model,
            max_tokens=8000,
            effort="low",
            timeout_seconds=s.provider_timeout_seconds,
        )
    if provider == "openai":
        if not s.autopilot_openai_model:
            raise ProviderNotConfiguredError("set AGENTOS_AUTOPILOT_OPENAI_MODEL to use OpenAI for Autopilot")
        return ModelConfig(
            model=s.autopilot_openai_model, max_tokens=8000, timeout_seconds=s.provider_timeout_seconds
        )
    raise ProviderNotConfiguredError(f"unknown provider {provider!r}")


async def cycle_spend(cycle_id: uuid.UUID) -> Decimal:
    async with get_sessionmaker()() as s:
        total = (
            await s.execute(
                select(func.coalesce(func.sum(ModelUsage.estimated_cost_usd), 0)).where(
                    ModelUsage.autopilot_ref == cycle_id
                )
            )
        ).scalar_one()
    return Decimal(str(total))


async def autopilot_call(
    *,
    provider: str,
    scenario: str,
    system: str,
    user_text: str,
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    purpose: str,
    cycle_id: uuid.UUID,
    max_cycle_spend_usd: float,
) -> tuple[ModelResponse, dict[str, Any]]:
    spent = await cycle_spend(cycle_id)
    if spent + RESERVATION_USD > Decimal(str(max_cycle_spend_usd)):
        raise BudgetExceededError(
            f"optimization cycle spend ${spent} would exceed the ${max_cycle_spend_usd} limit",
            details={"spent_usd": str(spent), "limit_usd": max_cycle_spend_usd},
        )
    config = autopilot_model(provider, scenario)
    status, error_type, resp = "succeeded", None, None
    try:
        await enforce_real_spend(provider, RESERVATION_USD)
        resp = await get_provider(provider).generate(
            system, [Message(role="user", text=user_text)], [], config
        )
    except Exception as exc:
        err = classify(exc)
        status, error_type = "failed", err.error_type
        await _record(
            workspace_id, agent_id, purpose, cycle_id, provider, config.model, None, status, error_type
        )
        raise err from exc
    await _record(workspace_id, agent_id, purpose, cycle_id, provider, resp.model, resp, status, error_type)
    return resp, {
        "provider": provider,
        "model": resp.model,
        "simulated": provider == "mock",
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }


async def _record(
    workspace_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    purpose: str,
    cycle_id: uuid.UUID,
    provider: str,
    model: str,
    resp: ModelResponse | None,
    status: str,
    error_type: str | None,
) -> None:
    u = resp.usage if resp else None
    est = get_pricing().estimate(
        provider,
        model,
        input_tokens=u.input_tokens if u else None,
        output_tokens=u.output_tokens if u else None,
        cached_input_tokens=u.cached_input_tokens if u else None,
    )
    async with unit_of_work() as s:
        s.add(
            ModelUsage(
                run_id=None,
                workspace_id=workspace_id,
                purpose=purpose,
                autopilot_ref=cycle_id,
                agent_definition_id=agent_id,
                provider=provider,
                model=model,
                input_tokens=u.input_tokens if u else None,
                output_tokens=u.output_tokens if u else None,
                cached_input_tokens=u.cached_input_tokens if u else None,
                latency_ms=resp.latency_ms if resp else 0,
                estimated_cost_usd=est.usd,
                cost_basis=est.basis,
                pricing_version=est.pricing_version,
                finish_reason=resp.finish_reason if resp else None,
                status=status,
                error_type=error_type,
                recorded_at=utcnow(),
            )
        )
