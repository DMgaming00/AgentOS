"""Versioned price table and cost estimation.

Costs are *estimates* computed from reported token usage. Unknown token counts
produce ``cost_basis='partial'`` or ``'unknown'`` rather than being treated as zero.
OpenAI prices are intentionally not bundled because they could not be verified
for this release; configure them in a custom pricing file to get estimates.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from agentos_core.config import get_settings

DEFAULT_PRICING = Path(__file__).with_name("pricing.json")
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    cached_input: Decimal | None
    cache_write: Decimal | None
    effective_date: str
    simulated: bool


@dataclass(frozen=True)
class CostEstimate:
    usd: Decimal | None
    basis: str  # actual | partial | simulated | unknown
    pricing_version: str


class PricingTable:
    def __init__(self, data: dict[str, Any]) -> None:
        self.version = str(data["version"])
        self.source = str(data.get("source", ""))
        self.prices: dict[str, Price] = {}
        for key, p in data["models"].items():
            self.prices[key] = Price(
                input=Decimal(str(p["input"])),
                output=Decimal(str(p["output"])),
                cached_input=Decimal(str(p["cached_input"])) if p.get("cached_input") is not None else None,
                cache_write=Decimal(str(p["cache_write"])) if p.get("cache_write") is not None else None,
                effective_date=str(p.get("effective_date", self.version)),
                simulated=bool(p.get("simulated", False)),
            )

    def lookup(self, provider: str, model: str) -> Price | None:
        return self.prices.get(f"{provider}/{model}") or self.prices.get(f"{provider}/*")

    def estimate(
        self,
        provider: str,
        model: str,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        cached_input_tokens: int | None = None,
        cache_write_tokens: int | None = None,
    ) -> CostEstimate:
        price = self.lookup(provider, model)
        if price is None or (input_tokens is None and output_tokens is None):
            return CostEstimate(None, "unknown", self.version)
        total = Decimal(0)
        complete = True
        if input_tokens is None:
            complete = False
        else:
            total += Decimal(input_tokens) * price.input / MILLION
        if output_tokens is None:
            complete = False
        else:
            total += Decimal(output_tokens) * price.output / MILLION
        if cached_input_tokens:
            rate = price.cached_input if price.cached_input is not None else price.input
            total += Decimal(cached_input_tokens) * rate / MILLION
        if cache_write_tokens:
            rate = price.cache_write if price.cache_write is not None else price.input
            total += Decimal(cache_write_tokens) * rate / MILLION
        basis = "simulated" if price.simulated else ("actual" if complete else "partial")
        return CostEstimate(total.quantize(Decimal("0.00000001")), basis, self.version)


@lru_cache
def get_pricing() -> PricingTable:
    path = get_settings().pricing_file or DEFAULT_PRICING
    return PricingTable(json.loads(Path(path).read_text()))
