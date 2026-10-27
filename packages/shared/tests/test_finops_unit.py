"""FinOps without a database: analytics arithmetic, every anomaly detector (fires and stays silent), suggestions, periods."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agentos_core.finops import ESTIMATE_LABEL, analytics, anomalies, suggestions
from agentos_core.finops.analytics import AttemptRow, Inputs, RunRow, StepRow, ToolRow, UsageRow
from agentos_core.finops.budgets import period_bounds
from agentos_core.pricing import PricingTable

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
AGENT_A, AGENT_B = uuid.uuid4(), uuid.uuid4()
PRICING = PricingTable(
    {
        "version": "test-1",
        "models": {
            "mock/big": {"input": 4.0, "output": 20.0, "simulated": True},
            "mock/small": {"input": 0.4, "output": 2.0, "simulated": True},
        },
    }
)


def use(
    minutes_ago=10,
    cost="0.01",
    agent=AGENT_A,
    name="A",
    model="big",
    run=None,
    attempt=None,
    latency=100,
    tin=1000,
    tout=100,
    status="succeeded",
    wf="wf",
    purpose="run",
):
    return UsageRow(
        NOW - timedelta(minutes=minutes_ago),
        run,
        attempt,
        agent,
        name,
        wf,
        "mock",
        model,
        tin,
        tout,
        latency,
        None if cost is None else Decimal(cost),
        "simulated",
        "test-1",
        status,
        purpose,
    )


def test_period_bounds_are_utc_calendar_periods():
    d = datetime(2026, 12, 31, 23, 59, tzinfo=UTC)
    assert period_bounds("daily", d) == (datetime(2026, 12, 31, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC))
    assert period_bounds("monthly", d) == (
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert period_bounds("monthly", datetime(2026, 2, 10, tzinfo=UTC))[1] == datetime(2026, 3, 1, tzinfo=UTC)


def test_summary_cost_per_success_retries_and_failed_runs():
    ok_run, bad_run = uuid.uuid4(), uuid.uuid4()
    failed_attempt = uuid.uuid4()
    inp = Inputs(
        usage=[
            use(cost="0.10", run=ok_run),
            use(cost="0.30", run=bad_run, attempt=failed_attempt),
            use(cost="0.20", run=bad_run),
            use(cost=None, run=ok_run),
        ],
        runs=[RunRow(ok_run, "wf", "succeeded", NOW), RunRow(bad_run, "wf", "failed", NOW)],
        steps=[
            StepRow(uuid.uuid4(), ok_run, AGENT_A, "succeeded"),
            StepRow(uuid.uuid4(), bad_run, AGENT_A, "failed"),
        ],
        attempts=[AttemptRow(failed_attempt, bad_run, AGENT_A, "failed", NOW, 50)],
        tools=[
            ToolRow(ok_run, AGENT_A, "document_search", "succeeded", "h"),
            ToolRow(ok_run, AGENT_A, "document_search", "failed", "h2"),
        ],
    )
    s = analytics.summarize(inp, now=NOW, days=7)
    assert s["label"] == ESTIMATE_LABEL and s["totals"]["estimated_cost_usd"] == 0.6
    assert s["totals"]["calls_with_unknown_cost"] == 1  # unknown is counted, not summed as zero
    assert s["totals"]["retry_cost_usd"] == 0.3 and s["totals"]["failed_run_cost_usd"] == 0.5
    a = s["by_agent"][0]
    assert (
        a["successful_steps"] == 1 and a["cost_per_successful_step_usd"] == 0.6 and a["retry_cost_usd"] == 0.3
    )
    wf = s["by_workflow"][0]
    assert wf["succeeded_runs"] == 1 and wf["cost_per_succeeded_run_usd"] == 0.6
    assert s["tools"][0] == {"tool": "document_search", "calls": 2, "failed": 1, "denied": 0}
    assert len(s["daily"]) == 7 and abs(sum(d["estimated_cost_usd"] for d in s["daily"]) - 0.6) < 1e-9
    assert s["totals"]["simulated_share"] == 1.0 and s["totals"]["pricing_versions"] == ["test-1"]


def test_cost_per_success_is_undefined_without_successes():
    s = analytics.summarize(
        Inputs(usage=[use()], steps=[StepRow(uuid.uuid4(), uuid.uuid4(), AGENT_A, "failed")]), now=NOW, days=7
    )
    assert s["by_agent"][0]["cost_per_successful_step_usd"] is None  # never reported as 0


def spread(n, hours_back=24, **kw):
    return [use(minutes_ago=90 + i * (hours_back * 60 - 120) // n, **kw) for i in range(n)]


def kinds(inp):
    return {a.kind for a in anomalies.detect(inp, now=NOW)}


def test_request_volume_spike_needs_history_and_volume():
    base = spread(48)  # 2 calls/hour for the previous day
    spike = [use(minutes_ago=m) for m in range(0, 40)]
    assert "request_volume_spike" in kinds(Inputs(usage=base + spike))
    assert "request_volume_spike" not in kinds(Inputs(usage=base + spike[:10]))  # below the minimum volume
    assert "request_volume_spike" not in kinds(
        Inputs(usage=spike)
    )  # no baseline: a new workspace is not a spike
    assert "request_volume_spike" not in kinds(
        Inputs(usage=spread(48) + [use(minutes_ago=m) for m in range(0, 3)])
    )


def test_spend_increase_compares_against_prior_days():
    days = [use(minutes_ago=24 * 60 * d + 600, cost="0.05") for d in range(2, 8) for _ in range(4)]
    spike = [use(minutes_ago=30 + i, cost="0.2") for i in range(5)]
    a = [x for x in anomalies.detect(Inputs(usage=days + spike), now=NOW) if x.kind == "spend_increase"]
    assert a and a[0].evidence["ratio"] >= 2 and a[0].severity in ("warning", "high")
    assert "spend_increase" not in kinds(Inputs(usage=days + [use(minutes_ago=30, cost="0.05")]))
    assert "spend_increase" not in kinds(Inputs(usage=days[:4] + spike))  # fewer than 3 baseline days


def test_abnormal_latency_per_model():
    base = [use(minutes_ago=24 * 60 * 3 + i, latency=100) for i in range(30)]
    slow = [use(minutes_ago=60 + i, latency=900) for i in range(25)]
    a = [x for x in anomalies.detect(Inputs(usage=base + slow), now=NOW) if x.kind == "abnormal_latency"]
    assert a and a[0].subject == "mock/big" and a[0].evidence["ratio"] >= 2
    assert "abnormal_latency" not in kinds(Inputs(usage=base + slow[:5]))


def test_excessive_retries_and_loops_and_low_success():
    attempts = [
        AttemptRow(
            uuid.uuid4(),
            uuid.uuid4(),
            AGENT_A,
            "failed" if i < 4 else "succeeded",
            NOW - timedelta(hours=1),
            None,
        )
        for i in range(10)
    ]
    assert "excessive_retries" in kinds(Inputs(usage=[use()], attempts=attempts))
    assert "excessive_retries" not in kinds(Inputs(usage=[use()], attempts=attempts[:9]))
    run = uuid.uuid4()
    loop = [ToolRow(run, AGENT_A, "document_search", "succeeded", "same") for _ in range(5)]
    found = [
        x
        for x in anomalies.detect(Inputs(usage=[use()], tools=loop), now=NOW)
        if x.kind == "repetitive_tool_loop"
    ]
    assert found and found[0].severity == "high"
    assert "repetitive_tool_loop" not in kinds(Inputs(usage=[use()], tools=loop[:4]))
    steps = [StepRow(uuid.uuid4(), uuid.uuid4(), AGENT_A, "failed") for _ in range(8)] + [
        StepRow(uuid.uuid4(), uuid.uuid4(), AGENT_A, "succeeded") for _ in range(2)
    ]
    usage = [use(cost="0.5", agent=AGENT_A, name="A")] + [use(cost="0.1", agent=AGENT_B, name="B")]
    assert "high_spend_low_success" in kinds(Inputs(usage=usage, steps=steps))
    ok = [StepRow(uuid.uuid4(), uuid.uuid4(), AGENT_A, "succeeded") for _ in range(10)]
    assert "high_spend_low_success" not in kinds(Inputs(usage=usage, steps=ok))
    assert "high_spend_low_success" not in kinds(
        Inputs(usage=usage, steps=steps[:5])
    )  # too few finished steps


def test_detection_is_deterministic_and_fingerprints_are_stable_within_a_bucket():
    inp = Inputs(usage=spread(48) + [use(minutes_ago=m) for m in range(0, 40)])
    a, b = anomalies.detect(inp, now=NOW), anomalies.detect(inp, now=NOW + timedelta(minutes=5))
    assert [x.fingerprint for x in a] == [
        x.fingerprint for x in b
    ] and a  # same hour bucket: one stored anomaly


def tool_rows(run, n, tool="document_search", h="h"):
    return [ToolRow(run, AGENT_A, tool, "succeeded", h) for _ in range(n)]


def test_suggestions_label_measured_projected_and_none_honestly():
    run = uuid.uuid4()
    failed = uuid.uuid4()
    usage = [use(cost="0.002", attempt=failed, model="big", tin=3000, tout=100) for _ in range(10)] + [
        use(cost="0.001", model="big", tin=3000, tout=100) for _ in range(60)
    ]
    inp = Inputs(
        usage=usage,
        attempts=[AttemptRow(failed, run, AGENT_A, "failed", NOW, 9000) for _ in range(1)]
        + [AttemptRow(uuid.uuid4(), run, AGENT_A, "succeeded", NOW, 9000) for _ in range(25)],
        tools=tool_rows(run, 8)
        + [ToolRow(uuid.uuid4(), AGENT_A, "document_search", "succeeded", "same") for _ in range(10)],
    )
    summary = analytics.summarize(inp, now=NOW, days=7)
    items = {
        s["id"]: s
        for s in suggestions.suggest(
            inp, summary, approved_models=[suggestions.ApprovedModel("mock", "small")], pricing=PRICING
        )
    }
    assert (
        items["bound_retries"]["savings"]["basis"] == "measured"
        and items["bound_retries"]["savings"]["usd"] == summary["totals"]["retry_cost_usd"]
    )
    cheaper = items["cheaper_approved_model"]["savings"]
    assert cheaper["basis"] == "projected" and "quality is unverified" in cheaper["note"]
    expected = (70 * 3000 * (4.0 - 0.4) + 70 * 100 * (20.0 - 2.0)) / 1_000_000
    assert abs(cheaper["usd"] - expected) < 1e-9
    assert items["reduce_redundant_tool_calls"]["savings"] == {
        "basis": "none",
        "usd": None,
        "note": items["reduce_redundant_tool_calls"]["savings"]["note"],
    }
    assert (
        items["reduce_context"]["savings"]["basis"] == "projected"
        and items["increase_capacity"]["savings"]["basis"] == "none"
    )
    assert "cache_read_only_tools" in items
    assert all(s["savings"]["basis"] in ("measured", "projected", "none") for s in items.values())


def test_replayed_calls_count_as_redundant_and_as_loops():
    run = uuid.uuid4()
    inp = Inputs(
        usage=[use() for _ in range(5)],
        tools=[ToolRow(run, AGENT_A, "document_search", "succeeded", f"h{i}", replays=1) for i in range(6)],
    )
    items = {
        s["id"]: s
        for s in suggestions.suggest(
            inp, analytics.summarize(inp, now=NOW, days=7), approved_models=[], pricing=PRICING
        )
    }
    assert items["reduce_redundant_tool_calls"]["evidence"] == {
        "redundant_calls": 6,
        "executed_twice": 0,
        "replayed_from_record": 6,
    }
    looped = Inputs(
        usage=[use()], tools=[ToolRow(run, AGENT_A, "document_search", "succeeded", "same", replays=4)]
    )
    assert "repetitive_tool_loop" in kinds(looped)  # 1 execution + 4 replays = 5 identical calls


def test_no_suggestions_without_evidence():
    inp = Inputs(usage=[use() for _ in range(5)])
    assert (
        suggestions.suggest(
            inp, analytics.summarize(inp, now=NOW, days=7), approved_models=[], pricing=PRICING
        )
        == []
    )
