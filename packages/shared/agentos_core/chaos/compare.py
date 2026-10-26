"""Before/after reliability comparison under the same fault schedule, using OpenEval's paired statistics."""

from __future__ import annotations

from typing import Any

from agentos_core.chaos.experiments import ChaosError
from agentos_core.db.models import ChaosExperiment
from openeval.metrics import CaseRecord
from openeval.stats import compare

NUMERIC = {
    "task_completion_rate": ("value", True),
    "recovery_rate": ("value", True),
    "state_consistency": ("value", True),
    "budget_compliance": ("value", True),
}


def compare_experiments(a: ChaosExperiment, b: ChaosExperiment) -> dict[str, Any]:
    for e in (a, b):
        if e.status != "completed" or not e.results:
            raise ChaosError(f"experiment {str(e.id)[:8]} has not completed", code="experiment_not_finished")
    if a.plan_sha256 != b.plan_sha256:
        raise ChaosError(
            "the two experiments used different plans, so they did not face the same fault schedule",
            code="different_plan",
        )
    assert a.results is not None and b.results is not None
    ra = [
        CaseRecord(case_id=str(c["case_index"]), attempt=0, passed=bool(c["completed"]))
        for c in a.results["per_case"]
    ]
    rb = [
        CaseRecord(case_id=str(c["case_index"]), attempt=0, passed=bool(c["completed"]))
        for c in b.results["per_case"]
    ]
    paired = compare(ra, rb, min_n=10)
    deltas: dict[str, Any] = {}
    for name in NUMERIC:
        x, y = a.results["metrics"][name], b.results["metrics"][name]
        deltas[name] = {
            "baseline": x,
            "candidate": y,
            "delta": (y["value"] - x["value"]) if x["value"] is not None and y["value"] is not None else None,
        }
    for name in ("time_to_detection_ms", "time_to_recovery_ms"):
        deltas[name] = {"baseline": a.results["metrics"][name], "candidate": b.results["metrics"][name]}
    deltas["retry_amplification"] = {
        "baseline": a.results["metrics"]["retry_amplification"],
        "candidate": b.results["metrics"]["retry_amplification"],
    }
    return {
        "baseline_id": str(a.id),
        "candidate_id": str(b.id),
        "same_fault_schedule": {
            "plan_sha256": a.plan_sha256,
            "seed": a.plan["seed"],
            "trials": a.plan["trials"],
            "note": "Fault decisions are a pure function of (seed, case, step, attempt, call), so "
            "both arms face the same schedule wherever the two configurations reach the same coordinates.",
        },
        "injections": {
            "baseline": a.results["injections_by_type"],
            "candidate": b.results["injections_by_type"],
        },
        "baseline_conditions": a.conditions,
        "candidate_conditions": b.conditions,
        "paired_completion": paired.to_dict(),
        "deltas": deltas,
        "caution": "Run-to-run variation exists (timing, worker assignment). Treat a difference as evidence only when the "
        "paired interval excludes 0 with enough cases; otherwise the comparison is inconclusive.",
    }
