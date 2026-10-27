"""Deterministic cost and behavior anomaly detection over usage rows.

Each detector compares a recent window against a trailing baseline with fixed, documented thresholds and minimum sample
sizes, so a quiet workspace or a brand-new agent does not raise alarms from a handful of calls. Detectors are pure functions of
``Inputs`` and ``now``; the persisted anomaly carries the numbers that triggered it, and a fingerprint (kind + subject + time
bucket) makes detection idempotent.
"""

from __future__ import annotations

import time
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import FinopsAnomaly
from agentos_core.finops.analytics import Inputs
from agentos_core.util import sha256_hex
from openeval.metrics import nearest_rank

THRESHOLDS: dict[str, Any] = {
    "volume_min_calls": 20,
    "volume_ratio": 3.0,
    "volume_baseline_hours": 24,
    "volume_baseline_min_calls": 20,
    "spend_ratio": 2.0,
    "spend_min_increase_usd": 0.01,
    "spend_baseline_days": 7,
    "spend_baseline_min_days": 3,
    "latency_ratio": 2.0,
    "latency_min_samples": 20,
    "retry_share": 0.30,
    "retry_min_attempts": 10,
    "loop_identical_calls": 5,
    "low_success_cost_share": 0.30,
    "low_success_rate": 0.50,
    "low_success_min_steps": 10,
}


@dataclass(frozen=True)
class Anomaly:
    kind: str
    severity: str
    subject: str
    summary: str
    evidence: dict[str, Any]
    fingerprint: str


def _fp(kind: str, subject: str, bucket: str) -> str:
    return sha256_hex(f"{kind}|{subject}|{bucket}")


def request_volume_spike(inp: Inputs, now: datetime, t: dict[str, Any] = THRESHOLDS) -> list[Anomaly]:
    last_start = now - timedelta(hours=1)
    base_start = now - timedelta(hours=1 + t["volume_baseline_hours"])
    last = sum(1 for r in inp.usage if r.at >= last_start)
    base = [r for r in inp.usage if base_start <= r.at < last_start]
    if len(base) < t["volume_baseline_min_calls"] or last < t["volume_min_calls"]:
        return []
    hourly = len(base) / t["volume_baseline_hours"]
    if last < hourly * t["volume_ratio"]:
        return []
    return [
        Anomaly(
            "request_volume_spike",
            "warning",
            "workspace",
            f"{last} model calls in the last hour vs {hourly:.1f}/hour over the previous {t['volume_baseline_hours']} hours",
            {
                "last_hour_calls": last,
                "baseline_hourly_calls": round(hourly, 2),
                "ratio": round(last / hourly, 2),
                "threshold_ratio": t["volume_ratio"],
            },
            _fp("request_volume_spike", "workspace", now.strftime("%Y%m%d%H")),
        )
    ]


def spend_increase(inp: Inputs, now: datetime, t: dict[str, Any] = THRESHOLDS) -> list[Anomaly]:
    last_start = now - timedelta(days=1)
    base_start = now - timedelta(days=1 + t["spend_baseline_days"])
    days: dict[str, Decimal] = defaultdict(Decimal)
    last = Decimal(0)
    for r in inp.usage:
        if r.cost is None:
            continue
        if r.at >= last_start:
            last += r.cost
        elif r.at >= base_start:
            days[r.at.date().isoformat()] += r.cost
    if len(days) < t["spend_baseline_min_days"]:
        return []
    mean = sum(days.values(), Decimal(0)) / len(days)
    if (
        mean <= 0
        or last < mean * Decimal(str(t["spend_ratio"]))
        or last - mean < Decimal(str(t["spend_min_increase_usd"]))
    ):
        return []
    return [
        Anomaly(
            "spend_increase",
            "high" if last >= mean * 5 else "warning",
            "workspace",
            f"Estimated spend in the last 24 hours is ${last:.4f}, {float(last / mean):.1f}x the daily mean of ${mean:.4f}",
            {
                "last_24h_usd": float(last),
                "baseline_daily_mean_usd": float(mean),
                "baseline_days_with_data": len(days),
                "ratio": round(float(last / mean), 2),
            },
            _fp("spend_increase", "workspace", now.strftime("%Y%m%d")),
        )
    ]


def abnormal_latency(inp: Inputs, now: datetime, t: dict[str, Any] = THRESHOLDS) -> list[Anomaly]:
    out = []
    last_start, base_start = (
        now - timedelta(days=1),
        now - timedelta(days=1 + THRESHOLDS["spend_baseline_days"]),
    )
    groups: dict[tuple[str, str], tuple[list[float], list[float]]] = defaultdict(lambda: ([], []))
    for r in inp.usage:
        if r.at >= last_start:
            groups[(r.provider, r.model)][0].append(float(r.latency_ms))
        elif r.at >= base_start:
            groups[(r.provider, r.model)][1].append(float(r.latency_ms))
    for (prov, model), (recent, base) in sorted(groups.items()):
        if len(recent) < t["latency_min_samples"] or len(base) < t["latency_min_samples"]:
            continue
        p_recent, p_base = nearest_rank(recent, 0.95), nearest_rank(base, 0.95)
        if p_recent is None or not p_base or p_recent < p_base * t["latency_ratio"]:
            continue
        out.append(
            Anomaly(
                "abnormal_latency",
                "warning",
                f"{prov}/{model}",
                f"p95 latency {p_recent:.0f} ms in the last 24 hours vs {p_base:.0f} ms baseline for {prov}/{model}",
                {
                    "recent_p95_ms": p_recent,
                    "baseline_p95_ms": p_base,
                    "recent_samples": len(recent),
                    "baseline_samples": len(base),
                    "ratio": round(p_recent / p_base, 2),
                },
                _fp("abnormal_latency", f"{prov}/{model}", now.strftime("%Y%m%d")),
            )
        )
    return out


def excessive_retries(inp: Inputs, now: datetime, t: dict[str, Any] = THRESHOLDS) -> list[Anomaly]:
    out = []
    start = now - timedelta(days=1)
    per: dict[uuid.UUID | None, list[str]] = defaultdict(list)
    for a in inp.attempts:
        if a.started_at >= start:
            per[a.agent_id].append(a.status)
    names = {u.agent_id: u.agent for u in inp.usage}
    for agent_id, statuses in per.items():
        failed = sum(s in ("failed", "lease_expired") for s in statuses)
        if len(statuses) >= t["retry_min_attempts"] and failed / len(statuses) >= t["retry_share"]:
            name = names.get(agent_id, str(agent_id))
            out.append(
                Anomaly(
                    "excessive_retries",
                    "warning",
                    name,
                    f"{failed} of {len(statuses)} attempts by {name} failed in the last 24 hours",
                    {
                        "attempts": len(statuses),
                        "failed": failed,
                        "share": round(failed / len(statuses), 3),
                        "threshold": t["retry_share"],
                    },
                    _fp("excessive_retries", str(agent_id), now.strftime("%Y%m%d")),
                )
            )
    return out


def repetitive_tool_loop(inp: Inputs, now: datetime, t: dict[str, Any] = THRESHOLDS) -> list[Anomaly]:
    groups: dict[tuple[uuid.UUID, str, str], int] = defaultdict(int)
    for tl in inp.tools:
        if tl.status == "succeeded":
            groups[(tl.run_id, tl.tool, tl.args_hash)] += 1 + tl.replays
    out = []
    for (run_id, tool, h), n in sorted(groups.items(), key=lambda kv: (-kv[1], str(kv[0]))):
        if n >= t["loop_identical_calls"]:
            out.append(
                Anomaly(
                    "repetitive_tool_loop",
                    "high",
                    f"{tool}@{str(run_id)[:8]}",
                    f"{tool} was called {n} times with identical arguments in one run",
                    {
                        "run_id": str(run_id),
                        "tool": tool,
                        "identical_calls": n,
                        "threshold": t["loop_identical_calls"],
                    },
                    _fp("repetitive_tool_loop", f"{run_id}|{tool}|{h}", "run"),
                )
            )
    return out[:5]


def high_spend_low_success(inp: Inputs, now: datetime, t: dict[str, Any] = THRESHOLDS) -> list[Anomaly]:
    total = sum((r.cost for r in inp.usage if r.cost is not None), Decimal(0))
    if total <= 0:
        return []
    cost: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    names: dict[uuid.UUID, str] = {}
    for r in inp.usage:
        if r.agent_id and r.cost is not None:
            cost[r.agent_id] += r.cost
            names[r.agent_id] = r.agent
    steps: dict[uuid.UUID, list[str]] = defaultdict(list)
    for s in inp.steps:
        if s.agent_id and s.status in ("succeeded", "failed"):
            steps[s.agent_id].append(s.status)
    out = []
    for aid, c in cost.items():
        done = steps.get(aid, [])
        if len(done) < t["low_success_min_steps"] or float(c / total) < t["low_success_cost_share"]:
            continue
        rate = sum(s == "succeeded" for s in done) / len(done)
        if rate < t["low_success_rate"]:
            out.append(
                Anomaly(
                    "high_spend_low_success",
                    "high",
                    names[aid],
                    f"{names[aid]} accounts for {float(c / total):.0%} of spend with a {rate:.0%} step success rate",
                    {
                        "agent_cost_usd": float(c),
                        "cost_share": round(float(c / total), 3),
                        "success_rate": round(rate, 3),
                        "finished_steps": len(done),
                    },
                    _fp("high_spend_low_success", str(aid), now.strftime("%Y%m%d")),
                )
            )
    return out


DETECTORS = (
    request_volume_spike,
    spend_increase,
    abnormal_latency,
    excessive_retries,
    repetitive_tool_loop,
    high_spend_low_success,
)


def detect(inp: Inputs, *, now: datetime, thresholds: dict[str, Any] | None = None) -> list[Anomaly]:
    t = {**THRESHOLDS, **(thresholds or {})}
    return [a for d in DETECTORS for a in d(inp, now, t)]


async def persist(session: AsyncSession, workspace_id: uuid.UUID, anomalies: list[Anomaly]) -> int:
    """Store new anomalies (idempotent by fingerprint); returns how many were new."""
    new = 0
    for a in anomalies:
        res = await session.execute(
            insert(FinopsAnomaly)
            .values(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                kind=a.kind,
                severity=a.severity,
                subject=a.subject,
                summary=a.summary,
                evidence=a.evidence,
                fingerprint=a.fingerprint,
            )
            .on_conflict_do_nothing(constraint="uq_finops_anomalies_fingerprint")
        )
        new += res.rowcount or 0  # type: ignore[attr-defined]
    return new


async def detect_for_workspace(
    session: AsyncSession, workspace_id: uuid.UUID, *, now: datetime | None = None
) -> tuple[list[Anomaly], int]:
    """Load the last 8 days of data, run every detector, persist new anomalies. Returns (all found, newly stored)."""
    from agentos_core.finops.analytics import load_inputs
    from agentos_core.util import utcnow

    now = now or utcnow()
    inp = await load_inputs(
        session, workspace_id, since=now - timedelta(days=1 + THRESHOLDS["spend_baseline_days"])
    )
    found = detect(inp, now=now)
    return found, await persist(session, workspace_id, found)


_last_tick = time.monotonic()  # the first detection runs one interval after start, not at start


async def finops_tick(*, interval_s: float = 300.0) -> int:
    """Coordinator duty (throttled per process): detect anomalies for workspaces with recent usage."""
    from sqlalchemy import select

    from agentos_core.db.models import ModelUsage, WorkflowRun
    from agentos_core.engine.tx import unit_of_work

    global _last_tick
    if time.monotonic() - _last_tick < interval_s:
        return 0
    _last_tick = time.monotonic()
    from agentos_core.util import utcnow

    since = utcnow() - timedelta(days=1)
    async with unit_of_work() as session:
        ws_ids = set(
            (
                await session.execute(
                    select(WorkflowRun.workspace_id)
                    .join(ModelUsage, ModelUsage.run_id == WorkflowRun.id)
                    .where(ModelUsage.recorded_at >= since)
                    .distinct()
                )
            ).scalars()
        )
        new = 0
        for ws in ws_ids:
            new += (await detect_for_workspace(session, ws))[1]
    return new
