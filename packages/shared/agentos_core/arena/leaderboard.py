"""Leaderboard statistics: pure functions over per-case outcomes, plus the loader that reads them from the database.

Scoring rules (all stated in the output, none hidden):

* a family's score is the share of its cases that pass *every* deterministic evaluator (OpenEval's definition);
* the overall score is the **unweighted mean of the family scores** (so a family with more cases does not dominate);
* intervals are 95%: Wilson for a single family's rate, a stratified percentile bootstrap (cases resampled within each
  family) for the overall score and for the difference between two contenders on the same cases;
* rank is **competition rank with statistical ties**: a contender's rank is 1 + the number of contenders whose overall score is
  higher by a margin whose paired bootstrap interval excludes zero. Contenders that cannot be told apart share a rank.
"""

from __future__ import annotations

import math
import random
import statistics
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agentos_core.db.models import (
    AgentConfigVersion,
    AgentDefinition,
    ArenaEntry,
    EvaluationCase,
    EvaluationCaseResult,
)
from openeval.metrics import nearest_rank

RESAMPLES = 2000
SEED = 20261026
OVERFIT_GAP = 0.10


@dataclass
class ContenderScores:
    agent_id: uuid.UUID
    agent_name: str
    version: int
    version_id: uuid.UUID
    config_hash: str
    # family -> case_key -> passed (a not-run or ungraded execution counts as a failure, never skipped)
    outcomes: dict[str, dict[str, bool]] = field(default_factory=dict)
    cost_usd: float | None = None
    latencies_ms: list[float] = field(default_factory=list)
    tool_calls: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    incomplete: int = 0


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def family_rates(outcomes: dict[str, dict[str, bool]]) -> dict[str, dict[str, Any]]:
    out = {}
    for family, cases in sorted(outcomes.items()):
        n, k = len(cases), sum(cases.values())
        ci = wilson(k, n)
        out[family] = {
            "n": n,
            "passed": k,
            "rate": (k / n) if n else None,
            "ci95": list(ci) if ci else None,
        }
    return out


def macro(outcomes: dict[str, dict[str, bool]]) -> float | None:
    rates = [sum(c.values()) / len(c) for c in outcomes.values() if c]
    return statistics.fmean(rates) if rates else None


def _bootstrap(
    a: ContenderScores, b: ContenderScores | None, *, resamples: int = RESAMPLES, seed: int = SEED
) -> tuple[float, float] | None:
    """95% interval of macro(a) (or of macro(a) - macro(b) on identical resampled cases)."""
    families = [f for f in sorted(a.outcomes) if a.outcomes[f] and (b is None or f in b.outcomes)]
    if not families:
        return None
    keys = {
        f: sorted(set(a.outcomes[f]) & set(b.outcomes[f])) if b else sorted(a.outcomes[f]) for f in families
    }
    rng = random.Random(seed)
    draws = []
    for _ in range(resamples):
        total = 0.0
        for f in families:
            ks = keys[f]
            if not ks:
                continue
            picks = [ks[rng.randrange(len(ks))] for _ in ks]
            ra = sum(a.outcomes[f][k] for k in picks) / len(picks)
            rb = (sum(b.outcomes[f][k] for k in picks) / len(picks)) if b else 0.0
            total += ra - rb
        draws.append(total / len(families))
    draws.sort()
    return draws[int(0.025 * resamples)], draws[min(resamples - 1, int(0.975 * resamples))]


def build_leaderboard(contenders: list[ContenderScores]) -> dict[str, Any]:
    scored = [(c, macro(c.outcomes)) for c in contenders]
    scored.sort(key=lambda t: (-(t[1] if t[1] is not None else -1.0), t[0].agent_name))
    rows: list[dict[str, Any]] = []
    for c, m in scored:
        ci = _bootstrap(c, None)
        cases = sum(len(v) for v in c.outcomes.values())
        passed = sum(sum(v.values()) for v in c.outcomes.values())
        rows.append(
            {
                "agent_id": str(c.agent_id),
                "agent": c.agent_name,
                "version": c.version,
                "agent_version_id": str(c.version_id),
                "config_hash": c.config_hash[:12],
                "overall": {"score": m, "ci95": list(ci) if ci else None},
                "families": family_rates(c.outcomes),
                "cases": cases,
                "passed": passed,
                "cost_usd": c.cost_usd,
                "cost_per_success_usd": (c.cost_usd / passed)
                if (c.cost_usd is not None and passed)
                else None,
                "latency_ms": {
                    "p50": nearest_rank(c.latencies_ms, 0.5),
                    "p95": nearest_rank(c.latencies_ms, 0.95),
                    "measured": len(c.latencies_ms),
                },
                "tool_calls": c.tool_calls,
                "failure_breakdown": dict(sorted(c.failures.items(), key=lambda kv: -kv[1])),
                "incomplete_executions": c.incomplete,
            }
        )
    # Rank with statistical ties: count contenders that are significantly better.
    by_id = {str(c.version_id): c for c, _ in scored}
    for row in rows:
        me = by_id[row["agent_version_id"]]
        better = 0
        for other_row in rows:
            if other_row is row:
                continue
            other = by_id[other_row["agent_version_id"]]
            ci = _bootstrap(other, me)
            if ci is not None and ci[0] > 0:
                better += 1
        row["rank"] = 1 + better
    pairs = []
    for i in range(len(rows) - 1):
        a, b = by_id[rows[i]["agent_version_id"]], by_id[rows[i + 1]["agent_version_id"]]
        ci = _bootstrap(a, b)
        d = (macro(a.outcomes) or 0) - (macro(b.outcomes) or 0)
        pairs.append(
            {
                "higher": rows[i]["agent"],
                "lower": rows[i + 1]["agent"],
                "difference": d,
                "ci95": list(ci) if ci else None,
                "separated": bool(ci and ci[0] > 0),
            }
        )
    return {
        "rows": rows,
        "adjacent_comparisons": pairs,
        "method": {
            "family_score": "share of cases passing every deterministic evaluator",
            "overall": "unweighted mean of family scores",
            "intervals": f"95%; Wilson per family, stratified bootstrap ({RESAMPLES} resamples, seed {SEED}) for overall and differences",
            "rank": "competition rank; ties when the paired bootstrap interval of the difference includes 0",
        },
    }


def selection_vs_test(selection: dict[str, Any], test: dict[str, Any]) -> list[dict[str, Any]]:
    """Per contender: validation score, test score, gap, and the two ranks."""
    test_by = {r["agent_version_id"]: r for r in test["rows"]}
    out = []
    for r in selection["rows"]:
        t = test_by.get(r["agent_version_id"])
        if t is None:
            continue
        v, s = r["overall"]["score"], t["overall"]["score"]
        gap = (v - s) if (v is not None and s is not None) else None
        out.append(
            {
                "agent": r["agent"],
                "agent_version_id": r["agent_version_id"],
                "validation_score": v,
                "test_score": s,
                "gap": gap,
                "validation_rank": r["rank"],
                "test_rank": t["rank"],
                "gap_flag": bool(gap is not None and gap > OVERFIT_GAP),
            }
        )
    return out


# ----------------------------------------------------------------------------- loading


async def load_scores(session: AsyncSession, arena_run_id: uuid.UUID, split: str) -> list[ContenderScores]:
    entries = list(
        (
            await session.execute(
                select(ArenaEntry).where(ArenaEntry.arena_run_id == arena_run_id, ArenaEntry.split == split)
            )
        ).scalars()
    )
    by_version: dict[uuid.UUID, ContenderScores] = {}
    for e in entries:
        c = by_version.get(e.agent_version_id)
        if c is None:
            agent = await session.get(AgentDefinition, e.agent_id)
            version = await session.get(AgentConfigVersion, e.agent_version_id)
            assert agent is not None and version is not None
            c = by_version[e.agent_version_id] = ContenderScores(
                agent_id=agent.id,
                agent_name=agent.name,
                version=version.version,
                version_id=version.id,
                config_hash=version.config_hash,
            )
        rows = (
            await session.execute(
                select(EvaluationCaseResult, EvaluationCase.case_key)
                .join(EvaluationCase, EvaluationCase.id == EvaluationCaseResult.case_id)
                .where(EvaluationCaseResult.evaluation_run_id == e.evaluation_run_id)
            )
        ).all()
        fam = c.outcomes.setdefault(e.family, {})
        for r, key in rows:
            # Not-run or still-running executions count as failures: a contender cannot gain by not finishing.
            fam[key] = bool(r.passed) and r.status == "graded"
            if r.status != "graded" or r.passed is None:
                c.incomplete += 1
            usage = r.usage or {}
            if usage.get("cost_usd") is not None:
                c.cost_usd = (c.cost_usd or 0.0) + float(usage["cost_usd"])
            if usage.get("latency_ms") is not None:
                c.latencies_ms.append(float(usage["latency_ms"]))
            c.tool_calls += int(usage.get("tool_calls") or 0)
            if r.passed is False:
                cat = (r.error or {}).get("category") or "evaluation_failed"
                c.failures[cat] = c.failures.get(cat, 0) + 1
    return list(by_version.values())
