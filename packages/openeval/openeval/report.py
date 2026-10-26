"""Exports. Every export carries dataset version, conditions and methodology."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

METHODOLOGY = (
    "Cases pass only if every deterministic evaluator passes; LLM-judge scores are advisory. "
    "Rates are reported with numerator/denominator. Cost per successful task is undefined when there "
    "are no successes. Paired comparisons use a seeded percentile bootstrap over matched cases."
)


def export_json(report: dict[str, Any]) -> str:
    return json.dumps({"methodology": METHODOLOGY, **report}, indent=2, sort_keys=True, default=str)


def export_csv(report: dict[str, Any]) -> str:
    buf = io.StringIO()
    meta = report.get("dataset", {})
    cond = report.get("conditions", {})
    w = csv.writer(buf)
    w.writerow(
        [
            f"# dataset={meta.get('name')} split={meta.get('split')} version={meta.get('version')} "
            f"hash={meta.get('hash')} agent_version={cond.get('agent_version')} model={cond.get('provider')}/"
            f"{cond.get('model')} methodology={METHODOLOGY}"
        ]
    )
    methods = sorted({m for r in report.get("results", []) for m in (r.get("scores") or {})})
    w.writerow(
        ["case_key", "attempt", "passed", *methods, "error", "cost_usd", "latency_ms", "workflow_run_id"]
    )
    for r in report.get("results", []):
        scores = r.get("scores") or {}
        w.writerow(
            [
                r["case_key"],
                r["attempt"],
                r["passed"],
                *[scores.get(m, {}).get("passed", "") for m in methods],
                (r.get("error") or {}).get("type", ""),
                (r.get("usage") or {}).get("cost_usd", ""),
                (r.get("usage") or {}).get("latency_ms", ""),
                r.get("workflow_run_id", ""),
            ]
        )
    return buf.getvalue()


def _pct(rate: dict[str, Any] | None) -> str:
    if not rate or rate.get("value") is None:
        return "n/a (0 samples)" if rate else "n/a"
    return f"{100 * rate['value']:.1f}% ({rate['numerator']}/{rate['denominator']})"


def export_markdown(report: dict[str, Any]) -> str:
    m, ds, cond = report.get("metrics") or {}, report.get("dataset", {}), report.get("conditions", {})
    lines = [
        f"# OpenEval report: {ds.get('name')} ({ds.get('split')} v{ds.get('version')})",
        "",
        f"- Dataset hash: `{ds.get('hash')}`",
        f"- Agent: {cond.get('agent_name')} v{cond.get('agent_version')} (config `{cond.get('config_hash', '')[:12]}`)",
        f"- Model: {cond.get('provider')}/{cond.get('model')}{' (SIMULATED mock provider)' if cond.get('simulated') else ''}",
        f"- Trials per case: {cond.get('trials')}; sampling seed: {cond.get('seed', 'not supported')}",
        f"- Executed: {cond.get('started_at')} to {cond.get('completed_at')}",
        "",
        "## Metrics",
        "",
        "| metric | value |",
        "|---|---|",
    ]
    for key in (
        "task_success_rate",
        "schema_compliance",
        "numeric_accuracy",
        "grounding_pass_rate",
        "tool_call_correctness",
        "failure_rate",
        "retry_rate",
    ):
        lines.append(f"| {key} | {_pct(m.get(key))} |")
    cps = m.get("cost_per_successful_task_usd")
    lat = m.get("latency_ms") or {}
    lines += [
        f"| cost per successful task | {'undefined (no successes)' if cps is None else f'${cps:.6f}'} |",
        f"| latency mean / p95 (ms) | {lat.get('mean')} / {lat.get('p95')} (n={lat.get('measured')}) |",
        "",
        "## Methodology",
        "",
        METHODOLOGY,
        "",
        "## Per-case results",
        "",
        "| case | attempt | passed | failing checks |",
        "|---|---|---|---|",
    ]
    for r in report.get("results", []):
        failing = [
            k for k, v in (r.get("scores") or {}).items() if not v.get("passed") and not v.get("advisory")
        ]
        lines.append(
            f"| {r['case_key']} | {r['attempt']} | {'yes' if r['passed'] else 'no'} | "
            f"{', '.join(failing) or (r.get('error') or {}).get('type', '')} |"
        )
    return "\n".join(lines) + "\n"
