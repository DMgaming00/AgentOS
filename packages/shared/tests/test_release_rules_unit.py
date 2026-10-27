"""Rollback rules as pure functions: each rule fires on its symptom, waits for evidence, and is deterministic."""

from datetime import UTC, datetime, timedelta

from agentos_core.release import rules as r
from agentos_core.release.rules import Arm, RollbackRules, Sample

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def samples(n, fail=0, latency=100.0, cost=0.001, spread_seconds=0, start_offset=0):
    return [
        Sample(
            at=NOW - timedelta(seconds=start_offset + (i * spread_seconds) // max(1, n)),
            ok=i >= fail,
            latency_ms=latency,
            cost_usd=cost,
        )
        for i in range(n)
    ]


def by_rule(canary, base, rules=None):
    return {x["rule"]: x for x in r.evaluate(rules or RollbackRules(), canary, base, now=NOW)}


def test_policy_violation_needs_no_statistics_but_needs_a_violation():
    assert by_rule(Arm([], 1), Arm([]))["critical_policy_violation"]["status"] == "triggered"
    assert by_rule(Arm(samples(50)), Arm(samples(50)))["critical_policy_violation"]["status"] == "ok"
    strict = RollbackRules.model_validate({"critical_policy_violation": {"min_events": 3}})
    assert by_rule(Arm([], 2), Arm([]), strict)["critical_policy_violation"]["status"] == "ok"


def test_failure_rate_waits_for_evidence_then_compares_arms():
    few = by_rule(Arm(samples(5, fail=5)), Arm(samples(50)))["failure_rate_increase"]
    assert few["status"] == "insufficient_evidence"  # 100% failures on 5 steps is not enough to act on
    hot = by_rule(Arm(samples(20, fail=6)), Arm(samples(40, fail=2)))["failure_rate_increase"]
    assert hot["status"] == "triggered" and hot["measured"]["difference"] == 0.25
    same = by_rule(Arm(samples(20, fail=2)), Arm(samples(40, fail=4)))["failure_rate_increase"]
    assert same["status"] == "ok"
    # a large rate difference from a single failure does not trigger (min_failures)
    one = by_rule(
        Arm(samples(10, fail=1)),
        Arm(samples(40)),
        RollbackRules.model_validate({"failure_rate_increase": {"max_increase": 0.05, "min_failures": 2}}),
    )["failure_rate_increase"]
    assert one["status"] == "ok"


def test_error_budget_from_a_success_slo():
    ok = by_rule(Arm(samples(40, fail=2)), Arm([]))["error_budget_exhausted"]
    assert ok["status"] == "ok" and ok["measured"]["allowed_failures"] == 2.0  # 5% of 40
    out = by_rule(Arm(samples(40, fail=3)), Arm([]))["error_budget_exhausted"]
    assert out["status"] == "triggered" and out["measured"]["budget_consumed"] == 1.5
    assert (
        by_rule(Arm(samples(10, fail=5)), Arm([]))["error_budget_exhausted"]["status"]
        == "insufficient_evidence"
    )


def windows(latency, per_window=6):
    out = []
    for k in range(3):
        for i in range(per_window):
            out.append(
                Sample(
                    at=NOW - timedelta(seconds=60 * k + 5 + i), ok=True, latency_ms=latency, cost_usd=0.001
                )
            )
    return out


def test_latency_regression_must_be_sustained_across_windows():
    slow, fast = windows(300.0), windows(100.0)
    assert by_rule(Arm(slow), Arm(fast))["sustained_latency_regression"]["status"] == "triggered"
    assert by_rule(Arm(fast), Arm(fast))["sustained_latency_regression"]["status"] == "ok"
    # one slow window among healthy ones is noise
    mixed = [s for s in slow if s.at > NOW - timedelta(seconds=60)] + [
        s for s in fast if s.at <= NOW - timedelta(seconds=60)
    ]
    assert by_rule(Arm(mixed), Arm(fast))["sustained_latency_regression"]["status"] == "ok"
    sparse = by_rule(Arm(slow[:6]), Arm(fast))["sustained_latency_regression"]
    assert sparse["status"] == "insufficient_evidence"


def test_spending_ratio_and_absolute_cap():
    hot = by_rule(Arm(samples(15, cost=0.01)), Arm(samples(15, cost=0.002)))["excessive_spending"]
    assert hot["status"] == "triggered" and hot["measured"]["ratio"] == 5.0
    assert (
        by_rule(Arm(samples(15, cost=0.003)), Arm(samples(15, cost=0.002)))["excessive_spending"]["status"]
        == "ok"
    )
    assert (
        by_rule(Arm(samples(5, cost=0.01)), Arm(samples(15, cost=0.002)))["excessive_spending"]["status"]
        == "insufficient_evidence"
    )
    capped = RollbackRules.model_validate({"excessive_spending": {"absolute_cap_usd": 0.05}})
    assert (
        by_rule(Arm(samples(12, cost=0.01)), Arm([]), capped)["excessive_spending"]["status"] == "triggered"
    )


def test_disabled_rules_never_fire_and_results_are_deterministic():
    off = RollbackRules.model_validate({k: {"enabled": False} for k in RollbackRules.model_fields})
    res = by_rule(Arm(samples(50, fail=50), 9), Arm(samples(50)), off)
    assert {v["status"] for v in res.values()} == {"disabled"}
    a = r.evaluate(RollbackRules(), Arm(samples(30, fail=9)), Arm(samples(30)), now=NOW)
    assert a == r.evaluate(RollbackRules(), Arm(samples(30, fail=9)), Arm(samples(30)), now=NOW)
