"""Paired comparison statistics for baseline vs candidate on matched cases."""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any

from openeval.metrics import CaseRecord

DEFAULT_SEED = 20261025


def per_case_success(records: list[CaseRecord]) -> dict[str, float]:
    """Mean success over trials for each case (repeated trials absorb nondeterminism)."""
    buckets: dict[str, list[bool]] = {}
    for r in records:
        buckets.setdefault(r.case_id, []).append(r.passed)
    return {k: sum(v) / len(v) for k, v in buckets.items()}


def paired_bootstrap(
    diffs: list[float], *, resamples: int = 10_000, seed: int = DEFAULT_SEED, alpha: float = 0.05
) -> tuple[float, float, float] | None:
    """Percentile bootstrap CI of the mean paired difference (resampling cases)."""
    n = len(diffs)
    if n == 0:
        return None
    rng = random.Random(seed)
    means = sorted(sum(diffs[rng.randrange(n)] for _ in range(n)) / n for _ in range(resamples))
    lo = means[int((alpha / 2) * resamples)]
    hi = means[min(resamples - 1, int((1 - alpha / 2) * resamples))]
    return statistics.fmean(diffs), lo, hi


def mcnemar_exact(b: int, c: int) -> float | None:
    """Two-sided exact McNemar p-value on discordant pairs (b: base pass/cand fail, c: reverse)."""
    n = b + c
    if n == 0:
        return None
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return float(min(1.0, 2 * p))


@dataclass
class Comparison:
    n_matched: int
    baseline_success: float | None
    candidate_success: float | None
    diff_mean: float | None
    ci: tuple[float, float] | None
    mcnemar_p: float | None
    regressions: list[str]
    improvements: list[str]
    unmatched: list[str]
    significant: bool
    interpretation: str
    per_case: list[dict[str, Any]] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_matched": self.n_matched,
            "baseline_success": self.baseline_success,
            "candidate_success": self.candidate_success,
            "diff_mean": self.diff_mean,
            "ci95": list(self.ci) if self.ci else None,
            "mcnemar_p": self.mcnemar_p,
            "regressions": self.regressions,
            "improvements": self.improvements,
            "unmatched": self.unmatched,
            "significant": self.significant,
            "interpretation": self.interpretation,
            "per_case": self.per_case,
            "settings": self.settings,
        }


def compare(
    baseline: list[CaseRecord],
    candidate: list[CaseRecord],
    *,
    min_n: int = 10,
    resamples: int = 10_000,
    seed: int = DEFAULT_SEED,
) -> Comparison:
    b, c = per_case_success(baseline), per_case_success(candidate)
    keys = sorted(set(b) & set(c))
    unmatched = sorted(set(b) ^ set(c))
    diffs = [c[k] - b[k] for k in keys]
    boot = paired_bootstrap(diffs, resamples=resamples, seed=seed)
    regressions = [k for k in keys if b[k] >= 0.5 > c[k]]
    improvements = [k for k in keys if c[k] >= 0.5 > b[k]]
    p = mcnemar_exact(len(regressions), len(improvements))
    ci = (boot[1], boot[2]) if boot else None
    significant = bool(ci) and len(keys) >= min_n and (ci[0] > 0 or ci[1] < 0)  # type: ignore[index]
    if not keys:
        text = "No matched cases: nothing can be concluded."
    elif len(keys) < min_n:
        text = f"Only {len(keys)} matched cases (< {min_n}): uncertainty is too high for a conclusion."
    elif significant and ci and ci[0] > 0:
        text = "Candidate success is higher; the 95% CI of the paired difference excludes 0."
    elif significant:
        text = "Candidate success is lower; the 95% CI of the paired difference excludes 0."
    else:
        text = "No statistically supported difference: the 95% CI of the paired difference includes 0."
    per_case = [{"case_id": k, "baseline": b[k], "candidate": c[k], "diff": c[k] - b[k]} for k in keys]
    return Comparison(
        n_matched=len(keys),
        baseline_success=statistics.fmean(b[k] for k in keys) if keys else None,
        candidate_success=statistics.fmean(c[k] for k in keys) if keys else None,
        diff_mean=boot[0] if boot else None,
        ci=ci,
        mcnemar_p=p,
        regressions=regressions,
        improvements=improvements,
        unmatched=unmatched,
        significant=significant,
        interpretation=text,
        per_case=per_case,
        settings={
            "method": "paired percentile bootstrap over cases",
            "resamples": resamples,
            "seed": seed,
            "alpha": 0.05,
            "min_n_for_significance": min_n,
            "secondary_test": "exact McNemar (two-sided)",
        },
    )
