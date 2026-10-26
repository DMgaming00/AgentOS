"""Deployment eligibility gates. Pure functions of measured experiment results.

Hard gates make a candidate ``not_eligible``; soft gates (uncertainty) make it
``inconclusive``. Lower cost or latency never compensates for a quality regression,
and "improved" is only claimed when the paired CI excludes zero.
"""

from __future__ import annotations

from typing import Any

from agentos_core.autopilot.policy import Gates


def _gate(name: str, passed: bool, severity: str, detail: str, **measured: Any) -> dict[str, Any]:
    return {"gate": name, "passed": passed, "severity": severity, "detail": detail, "measured": measured}


def evaluate_gates(
    *,
    comparison: dict[str, Any],
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    gates: Gates,
    denied_tool_calls: int,
    critical_regressions: list[str],
) -> dict[str, Any]:
    n = comparison["n_matched"]
    ci = comparison.get("ci95")
    planned = candidate.get("planned_executions") or 0
    incomplete = (candidate.get("incomplete_executions") or 0) + (baseline.get("incomplete_executions") or 0)
    incomplete_rate = incomplete / max(1, planned + (baseline.get("planned_executions") or 0))
    cand_success = (candidate.get("task_success_rate") or {}).get("value")
    b_cps, c_cps = baseline.get("cost_per_successful_task_usd"), candidate.get("cost_per_successful_task_usd")
    out = [
        _gate(
            "sample_size",
            n >= gates.min_validation_cases,
            "soft",
            f"{n} matched cases (minimum {gates.min_validation_cases})",
            n=n,
        ),
        _gate(
            "evaluation_complete",
            incomplete_rate <= gates.max_incomplete_rate,
            "soft",
            f"{incomplete} executions did not run",
            incomplete_rate=round(incomplete_rate, 4),
        ),
        _gate(
            "no_unauthorized_tool_behavior",
            denied_tool_calls == 0,
            "hard",
            f"{denied_tool_calls} tool calls denied by policy during the candidate's evaluation",
            denied=denied_tool_calls,
        ),
        _gate(
            "per_case_regressions",
            len(comparison["regressions"]) <= gates.max_regressions,
            "hard",
            f"{len(comparison['regressions'])} cases pass on baseline but fail on candidate "
            f"(allowed {gates.max_regressions})",
            regressions=comparison["regressions"],
        ),
        _gate(
            "critical_case_regressions",
            not critical_regressions,
            "hard",
            f"regressions on cases tagged {gates.no_regression_tags}: {critical_regressions}",
        ),
        _gate(
            "no_supported_quality_degradation",
            not (ci and ci[1] < 0),
            "hard",
            "95% CI of the success difference lies entirely below 0"
            if ci and ci[1] < 0
            else "no statistically supported degradation",
            ci95=ci,
        ),
        _gate(
            "non_inferiority",
            ci is not None and ci[0] >= -gates.noninferiority_margin,
            "soft",
            f"lower CI bound {ci[0]:.3f} vs margin -{gates.noninferiority_margin}"
            if ci
            else "no CI available",
            ci95=ci,
        ),
        _gate(
            "minimum_success_rate",
            cand_success is not None and cand_success >= gates.min_candidate_success_rate,
            "hard",
            f"candidate success {cand_success} (minimum {gates.min_candidate_success_rate})",
            success=cand_success,
        ),
    ]
    if c_cps is None:
        out.append(
            _gate("cost_per_successful_task", False, "hard", "undefined: the candidate had no successes")
        )
    else:
        within_budget = c_cps <= gates.max_cost_per_success_usd
        vs_base = b_cps is None or c_cps <= b_cps * (1 + gates.max_cost_per_success_increase)
        out.append(
            _gate(
                "cost_per_successful_task",
                within_budget and vs_base,
                "hard",
                f"${c_cps:.6f} per success vs baseline "
                f"{'undefined' if b_cps is None else f'${b_cps:.6f}'} (max +{gates.max_cost_per_success_increase:.0%}, "
                f"cap ${gates.max_cost_per_success_usd})",
                candidate=c_cps,
                baseline=b_cps,
            )
        )
    hard_fail = [g for g in out if not g["passed"] and g["severity"] == "hard"]
    soft_fail = [g for g in out if not g["passed"] and g["severity"] == "soft"]
    decision = "not_eligible" if hard_fail else ("inconclusive" if soft_fail else "eligible")
    improved = bool(comparison.get("significant") and ci and ci[0] > 0)
    return {
        "decision": decision,
        "improvement_demonstrated": improved,
        "summary": ("Not eligible: " + "; ".join(g["gate"] for g in hard_fail))
        if hard_fail
        else ("Inconclusive: " + "; ".join(g["gate"] for g in soft_fail))
        if soft_fail
        else (
            "Eligible; improvement demonstrated (95% CI excludes 0)."
            if improved
            else "Eligible (non-inferior); no improvement demonstrated."
        ),
        "gates": out,
    }
