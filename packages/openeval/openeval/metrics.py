"""Aggregate metrics. Every rate is reported with its numerator and denominator."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any


@dataclass
class CaseRecord:
    case_id: str
    attempt: int
    passed: bool
    method_passed: dict[str, bool] = field(default_factory=dict)
    errored: bool = False  # the execution itself failed (no usable output)
    error_category: str | None = None
    cost_usd: float | None = None
    latency_ms: float | None = None
    retries: int = 0


def _rate(num: int, den: int) -> dict[str, Any]:
    return {"value": (num / den) if den else None, "numerator": num, "denominator": den}


def nearest_rank(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def aggregate(records: list[CaseRecord]) -> dict[str, Any]:
    n = len(records)
    successes = sum(r.passed for r in records)

    def method_rate(m: str) -> dict[str, Any]:
        rs = [r.method_passed[m] for r in records if m in r.method_passed]
        return _rate(sum(rs), len(rs))

    costs = [r.cost_usd for r in records if r.cost_usd is not None]
    lat = [r.latency_ms for r in records if r.latency_ms is not None]
    total_cost = sum(costs) if costs else None
    errors: dict[str, int] = {}
    for r in records:
        if not r.passed:
            key = r.error_category or ("execution_error" if r.errored else "evaluation_failed")
            errors[key] = errors.get(key, 0) + 1
    return {
        "attempts": n,
        "cases": len({r.case_id for r in records}),
        "task_success_rate": _rate(successes, n),
        "tool_call_correctness": method_rate("tool_correctness"),
        "schema_compliance": method_rate("schema_validation"),
        "grounding_pass_rate": method_rate("source_citation_check"),
        "numeric_accuracy": method_rate("numeric_tolerance"),
        "failure_rate": _rate(sum(r.errored for r in records), n),
        "retry_rate": _rate(sum(r.retries > 0 for r in records), n),
        "total_cost_usd": total_cost,
        "cost_measured_attempts": len(costs),
        # undefined (None) when nothing succeeded - never reported as 0
        "cost_per_successful_task_usd": (total_cost / successes)
        if (successes and total_cost is not None)
        else None,
        "latency_ms": {
            "mean": statistics.fmean(lat) if lat else None,
            "p50": nearest_rank(lat, 0.5),
            "p95": nearest_rank(lat, 0.95),
            "measured": len(lat),
        },
        "failure_breakdown": dict(sorted(errors.items(), key=lambda kv: -kv[1])),
    }
