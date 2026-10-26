"""Central execution-mode boundary for every model request.

Paid providers are blocked in code unless ALL of these hold:

* ``AGENTOS_EXECUTION_MODE=live``
* ``AGENTOS_ALLOW_PAID_PROVIDERS=true``
* ``AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS=true``
* ``AGENTOS_MAX_REAL_API_SPEND_USD`` > 0, with recorded real spend below it

The check runs in two independent places so that no single bypass reaches the network:

1. ``check_provider_call`` at the start of every adapter ``generate`` (before any request is built).
2. ``GuardedTransport`` under the real SDK clients: even calling ``provider.client`` directly cannot
   open a connection while offline.

Having an API key present never changes the outcome. ``mock`` and ``fixture`` providers cannot reach
the network and are always allowed; an SDK client whose transport is an in-memory mock transport
(contract tests) is allowed for the same reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx2

from agentos_core.config import get_settings
from agentos_core.domain.errors import BudgetExceededError, PermanentError

NETWORKLESS_PROVIDERS = frozenset({"mock", "fixture", "recorded"})
REAL_PROVIDERS = frozenset({"anthropic", "openai"})


class OfflineModeError(PermanentError):
    """A request that would reach a paid provider was refused before any connection was attempted."""

    error_type = "provider_blocked_offline"


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: str
    allow_paid_providers: bool
    network_model_requests: bool
    max_real_spend_usd: float

    @classmethod
    def current(cls) -> ExecutionPolicy:
        s = get_settings()
        return cls(
            s.execution_mode,
            s.allow_paid_providers,
            s.enable_network_model_requests,
            s.max_real_api_spend_usd,
        )

    def blockers(self) -> list[str]:
        out = []
        if self.mode != "live":
            out.append("AGENTOS_EXECUTION_MODE is not 'live'")
        if not self.allow_paid_providers:
            out.append("AGENTOS_ALLOW_PAID_PROVIDERS is not true")
        if not self.network_model_requests:
            out.append("AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS is not true")
        if self.max_real_spend_usd <= 0:
            out.append("AGENTOS_MAX_REAL_API_SPEND_USD is 0")
        return out

    @property
    def paid_requests_allowed(self) -> bool:
        return not self.blockers()


def check_provider_call(provider: str, model: str | None = None, *, network_capable: bool = True) -> None:
    """Raise ``OfflineModeError`` unless a request to ``provider`` may leave the process."""
    if provider in NETWORKLESS_PROVIDERS or not network_capable:
        return
    blockers = ExecutionPolicy.current().blockers()
    if blockers:
        raise OfflineModeError(
            f"request to {provider}{f' ({model})' if model else ''} blocked before any network "
            f"connection: {'; '.join(blockers)}. See docs/ENABLE_REAL_CLAUDE.md.",
            details={"provider": provider, "model": model, "blockers": blockers},
        )


def client_is_networkless(client: Any) -> bool:
    """True when an SDK client's HTTP transport is an in-memory mock (contract tests)."""
    transport = getattr(getattr(client, "_client", None), "_transport", None)
    return isinstance(transport, httpx2.MockTransport)


class GuardedTransport(httpx2.AsyncBaseTransport):
    """Transport placed under real SDK clients; every request re-checks the execution policy."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self._inner = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        check_provider_call(self.provider, None)
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


async def real_spend_usd() -> Decimal:
    """Recorded spend on real providers (``model_usage``), the ledger the spend cap is enforced against."""
    from sqlalchemy import func, select

    from agentos_core.db.models import ModelUsage
    from agentos_core.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        total = (
            await s.execute(
                select(func.coalesce(func.sum(ModelUsage.estimated_cost_usd), 0)).where(
                    ModelUsage.provider.in_(sorted(REAL_PROVIDERS))
                )
            )
        ).scalar_one()
    return Decimal(str(total))


async def enforce_real_spend(provider: str, reservation: Decimal) -> None:
    """Refuse a real-provider request that could push recorded spend over the configured cap."""
    if provider not in REAL_PROVIDERS:
        return
    check_provider_call(provider)
    cap = Decimal(str(get_settings().max_real_api_spend_usd))
    spent = await real_spend_usd()
    if spent + reservation > cap:
        raise BudgetExceededError(
            f"real-provider spend ${spent} + reservation ${reservation} would exceed the ${cap} cap",
            details={"spent_usd": str(spent), "cap_usd": str(cap)},
        )


def execution_status() -> dict[str, object]:
    p = ExecutionPolicy.current()
    return {
        "mode": p.mode,
        "paid_requests_allowed": p.paid_requests_allowed,
        "blockers": p.blockers(),
        "max_real_api_spend_usd": p.max_real_spend_usd,
        "label": "Simulated (offline): no paid provider can be called"
        if not p.paid_requests_allowed
        else "Live providers enabled",
    }
