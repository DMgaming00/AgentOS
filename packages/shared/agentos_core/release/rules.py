"""Rollback rules: deterministic functions of measured canary-vs-baseline samples.

Each rule needs a *minimum evidence window* before it may fire, so random noise in a handful of runs cannot trigger a
rollback; below the minimum the rule reports ``insufficient_evidence`` (never "ok"). Rules only read measurements; no model
is consulted, and the same samples always give the same verdicts.

* ``critical_policy_violation``: tool calls denied by policy on the canary arm (default: 1 is enough, because a policy
  violation is not a statistical question);
* ``failure_rate_increase``: canary failure rate minus baseline failure rate over at least N steps per arm;
* ``error_budget_exhausted``: failures on the canary arm exceed the budget implied by a success-rate SLO;
* ``sustained_latency_regression``: canary p95 latency exceeds baseline p95 by a ratio in *each* of K consecutive windows;
* ``excessive_spending``: canary cost per step exceeds baseline cost per step by a ratio (or an absolute cap on the arm).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from openeval.metrics import nearest_rank


class Sample(BaseModel):
    """One finished live step."""

    at: datetime
    ok: bool
    latency_ms: float | None = None
    cost_usd: float | None = None


@dataclass
class Arm:
    samples: list[Sample]
    denied_tool_calls: int = 0


class CriticalPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    min_events: int = Field(default=1, ge=1)


class FailureRate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_increase: float = Field(default=0.15, ge=0, le=1)
    min_samples: int = Field(default=10, ge=1)
    min_failures: int = Field(default=2, ge=1)
    # one-sided Fisher exact test: the excess must also be unlikely under "the two arms fail at the same rate"
    alpha: float = Field(default=0.05, gt=0, lt=1)


class ErrorBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    slo_success: float = Field(default=0.95, gt=0, lt=1)
    min_samples: int = Field(default=20, ge=1)
    # one-sided binomial test: the failures must be unlikely if the arm were exactly at the SLO boundary
    alpha: float = Field(default=0.05, gt=0, lt=1)


class Latency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_ratio: float = Field(default=1.5, gt=1)
    windows: int = Field(default=3, ge=1, le=10)
    window_seconds: int = Field(default=60, ge=1)
    min_samples_per_window: int = Field(default=5, ge=1)


class Spending(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = True
    max_cost_ratio: float = Field(default=2.0, gt=1)
    min_samples: int = Field(default=10, ge=1)
    absolute_cap_usd: float | None = Field(default=None, gt=0)


class RollbackRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    critical_policy_violation: CriticalPolicy = Field(default_factory=CriticalPolicy)
    failure_rate_increase: FailureRate = Field(default_factory=FailureRate)
    error_budget_exhausted: ErrorBudget = Field(default_factory=ErrorBudget)
    sustained_latency_regression: Latency = Field(default_factory=Latency)
    excessive_spending: Spending = Field(default_factory=Spending)


def binomial_tail(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exactly."""
    return min(1.0, sum(math.comb(n, i) * p**i * (1 - p) ** (n - i) for i in range(k, n + 1)))


def fisher_tail(canary_fail: int, canary_n: int, base_fail: int, base_n: int) -> float:
    """One-sided Fisher exact p-value: probability of at least ``canary_fail`` failures in the canary arm if both arms share one failure rate."""
    total_fail, total_n = canary_fail + base_fail, canary_n + base_n
    denom = math.comb(total_n, canary_n)
    hi = min(canary_n, total_fail)
    return min(
        1.0,
        sum(
            math.comb(total_fail, i) * math.comb(total_n - total_fail, canary_n - i)
            for i in range(canary_fail, hi + 1)
        )
        / denom,
    )


def _result(rule: str, status: str, reason: str, **measured: Any) -> dict[str, Any]:
    return {"rule": rule, "status": status, "reason": reason, "measured": measured}


def _rate(samples: list[Sample]) -> float:
    return sum(not s.ok for s in samples) / len(samples)


def critical_policy_violation(cfg: CriticalPolicy, canary: Arm, base: Arm) -> dict[str, Any]:
    name = "critical_policy_violation"
    if not cfg.enabled:
        return _result(name, "disabled", "rule disabled")
    n = canary.denied_tool_calls
    if n >= cfg.min_events:
        return _result(
            name,
            "triggered",
            f"{n} tool calls on the canary were denied by policy (threshold {cfg.min_events})",
            denied=n,
            baseline_denied=base.denied_tool_calls,
        )
    return _result(name, "ok", "no policy violations on the canary", denied=n)


def failure_rate_increase(cfg: FailureRate, canary: Arm, base: Arm) -> dict[str, Any]:
    name = "failure_rate_increase"
    if not cfg.enabled:
        return _result(name, "disabled", "rule disabled")
    c, b = canary.samples, base.samples
    if len(c) < cfg.min_samples or len(b) < cfg.min_samples:
        return _result(
            name,
            "insufficient_evidence",
            f"needs {cfg.min_samples} steps per arm (canary {len(c)}, baseline {len(b)})",
            canary_n=len(c),
            baseline_n=len(b),
        )
    cr, br = _rate(c), _rate(b)
    failures = sum(not s.ok for s in c)
    diff = cr - br
    measured = {
        "canary_failure_rate": round(cr, 4),
        "baseline_failure_rate": round(br, 4),
        "difference": round(diff, 4),
        "canary_failures": failures,
        "canary_n": len(c),
        "baseline_n": len(b),
    }
    if diff >= cfg.max_increase and failures >= cfg.min_failures:
        p = fisher_tail(failures, len(c), sum(not x.ok for x in b), len(b))
        measured["fisher_p"] = round(p, 5)
        if p >= cfg.alpha:
            return _result(
                name,
                "insufficient_evidence",
                f"canary {cr:.0%} vs baseline {br:.0%} exceeds +{cfg.max_increase:.0%}, but the excess is within what noise "
                f"would produce (Fisher p={p:.3f} >= {cfg.alpha}); more steps needed",
                **measured,
            )
        return _result(
            name,
            "triggered",
            f"canary failure rate {cr:.0%} vs baseline {br:.0%} (+{diff:.0%}, limit +{cfg.max_increase:.0%}; Fisher p={p:.4f})",
            **measured,
        )
    return _result(
        name, "ok", f"canary {cr:.0%} vs baseline {br:.0%} within +{cfg.max_increase:.0%}", **measured
    )


def error_budget_exhausted(cfg: ErrorBudget, canary: Arm, base: Arm) -> dict[str, Any]:
    name = "error_budget_exhausted"
    if not cfg.enabled:
        return _result(name, "disabled", "rule disabled")
    n = len(canary.samples)
    if n < cfg.min_samples:
        return _result(
            name, "insufficient_evidence", f"needs {cfg.min_samples} canary steps (have {n})", canary_n=n
        )
    failures = sum(not s.ok for s in canary.samples)
    allowed = (1 - cfg.slo_success) * n
    consumed = failures / allowed if allowed else float("inf")
    measured = {
        "canary_n": n,
        "failures": failures,
        "allowed_failures": round(allowed, 3),
        "budget_consumed": round(consumed, 3),
        "slo_success": cfg.slo_success,
    }
    if failures > allowed:
        p = binomial_tail(failures, n, 1 - cfg.slo_success)
        measured["binomial_p"] = round(p, 5)
        if p >= cfg.alpha:
            return _result(
                name,
                "insufficient_evidence",
                f"{failures} failures exceed the error budget of {allowed:.2f}, but that is within noise for an arm exactly at the "
                f"{cfg.slo_success:.0%} SLO (p={p:.3f} >= {cfg.alpha}); more steps needed",
                **measured,
            )
        return _result(
            name,
            "triggered",
            f"{failures} failures exceed the error budget of {allowed:.2f} for a {cfg.slo_success:.0%} success SLO over {n} steps (binomial p={p:.4f})",
            **measured,
        )
    return _result(name, "ok", f"{consumed:.0%} of the error budget consumed", **measured)


def sustained_latency_regression(cfg: Latency, canary: Arm, base: Arm, *, now: datetime) -> dict[str, Any]:
    name = "sustained_latency_regression"
    if not cfg.enabled:
        return _result(name, "disabled", "rule disabled")
    ratios: list[float | None] = []
    for k in range(cfg.windows):
        end = now - timedelta(seconds=cfg.window_seconds * k)
        start = end - timedelta(seconds=cfg.window_seconds)
        c = [s.latency_ms for s in canary.samples if s.latency_ms is not None and start < s.at <= end]
        b = [s.latency_ms for s in base.samples if s.latency_ms is not None and start < s.at <= end]
        if len(c) < cfg.min_samples_per_window or len(b) < cfg.min_samples_per_window:
            ratios.append(None)
            continue
        pc, pb = nearest_rank(c, 0.95), nearest_rank(b, 0.95)
        ratios.append((pc / pb) if (pb and pc is not None) else None)
    if any(r is None for r in ratios):
        have = sum(r is not None for r in ratios)
        return _result(
            name,
            "insufficient_evidence",
            f"needs {cfg.windows} consecutive windows of {cfg.window_seconds}s with {cfg.min_samples_per_window}+ steps per arm (have {have})",
            window_ratios=ratios,
        )
    if all(r is not None and r >= cfg.max_ratio for r in ratios):
        return _result(
            name,
            "triggered",
            f"canary p95 latency was at least {cfg.max_ratio}x the baseline in each of the last {cfg.windows} windows",
            window_ratios=[round(r, 2) for r in ratios if r is not None],
        )
    return _result(
        name,
        "ok",
        "latency regression is not sustained across windows",
        window_ratios=[round(r, 2) for r in ratios if r is not None],
    )


def excessive_spending(cfg: Spending, canary: Arm, base: Arm) -> dict[str, Any]:
    name = "excessive_spending"
    if not cfg.enabled:
        return _result(name, "disabled", "rule disabled")
    c = [s.cost_usd for s in canary.samples if s.cost_usd is not None]
    b = [s.cost_usd for s in base.samples if s.cost_usd is not None]
    total = sum(c)
    if cfg.absolute_cap_usd is not None and total > cfg.absolute_cap_usd and len(c) >= cfg.min_samples:
        return _result(
            name,
            "triggered",
            f"canary spend ${total:.6f} exceeds the ${cfg.absolute_cap_usd} cap",
            canary_spend_usd=total,
            cap_usd=cfg.absolute_cap_usd,
        )
    if len(c) < cfg.min_samples or len(b) < cfg.min_samples:
        return _result(
            name,
            "insufficient_evidence",
            f"needs {cfg.min_samples} steps with known cost per arm (canary {len(c)}, baseline {len(b)})",
            canary_n=len(c),
            baseline_n=len(b),
        )
    cm, bm = total / len(c), sum(b) / len(b)
    if bm > 0 and cm / bm >= cfg.max_cost_ratio:
        return _result(
            name,
            "triggered",
            f"canary cost per step ${cm:.6f} is {cm / bm:.1f}x the baseline's ${bm:.6f} (limit {cfg.max_cost_ratio}x)",
            canary_per_step_usd=cm,
            baseline_per_step_usd=bm,
            ratio=round(cm / bm, 3),
        )
    return _result(
        name,
        "ok",
        f"canary cost per step ${cm:.6f} vs baseline ${bm:.6f}",
        canary_per_step_usd=cm,
        baseline_per_step_usd=bm,
    )


def evaluate(rules: RollbackRules, canary: Arm, base: Arm, *, now: datetime) -> list[dict[str, Any]]:
    return [
        critical_policy_violation(rules.critical_policy_violation, canary, base),
        failure_rate_increase(rules.failure_rate_increase, canary, base),
        error_budget_exhausted(rules.error_budget_exhausted, canary, base),
        sustained_latency_regression(rules.sustained_latency_regression, canary, base, now=now),
        excessive_spending(rules.excessive_spending, canary, base),
    ]
