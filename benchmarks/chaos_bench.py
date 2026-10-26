"""Chaos Lab benchmark: resilience scorecards for every built-in scenario, with and without retries.

Real worker processes (a crashed worker is replaced by a supervisor), an isolated database, the deterministic mock
provider. The numbers describe how the *platform* behaves under injected faults, not how a real model behaves.

    cd benchmarks && uv run --group bench python chaos_bench.py --label chaos [--quick]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
import uuid
from pathlib import Path
from typing import Any

from harness import create_database, machine_info, reset, workers

from agentos_core.chaos.compare import compare_experiments
from agentos_core.chaos.experiments import create_experiment
from agentos_core.chaos.faults import PRESETS, ChaosPlan
from agentos_core.db.models import ChaosExperiment
from agentos_core.db.session import dispose_engine, get_sessionmaker
from agentos_core.demo import research_report_workflow
from agentos_core.engine.tx import unit_of_work
from agentos_core.testing import add_agent, save_workflow

OUT = Path(__file__).parent / "results"
POLICIES = ("no_retry", "as_configured", "extra_retries")
FAST = {"max_attempts": 3, "backoff_base_seconds": 0.2, "backoff_max_seconds": 0.5}
ENV = {
    "AGENTOS_LEASE_TTL_SECONDS": "3",
    "AGENTOS_HEARTBEAT_INTERVAL_SECONDS": "0.6",
    "AGENTOS_COORDINATOR_INTERVAL_SECONDS": "0.3",
}


def research_workflow() -> dict[str, Any]:
    nodes = [
        {
            "key": f"r{i}",
            "type": "agent_task",
            "agent": "Research Agent",
            "tools": ["document_search"],
            "input": {"platform": p},
            "retry": FAST,
        }
        for i, p in enumerate(["Nimbus Inference", "Helix Serve", "Vector Forge"])
    ]
    return {"nodes": [*nodes, {"key": "agg", "type": "aggregation", "depends_on": [n["key"] for n in nodes]}]}


def hostile_workflow() -> dict[str, Any]:
    return {
        "nodes": [
            {
                "key": "g",
                "type": "agent_task",
                "agent": "Gullible Agent",
                "tools": ["document_search"],
                "retry": FAST,
            }
        ]
    }


SCENARIOS: dict[str, tuple[str, dict[str, Any]]] = {
    "control (no faults)": (
        "research",
        {"faults": [{"type": "tool_failure", "probability": 0.0}], "seed": 1},
    ),
    "provider-brownout": ("research", PRESETS["provider-brownout"]["plan"]),
    "garbage-model-output": ("research", PRESETS["garbage-model-output"]["plan"]),
    "flaky-tools": ("research", PRESETS["flaky-tools"]["plan"]),
    "worker-crash-loop": ("research", PRESETS["worker-crash-loop"]["plan"]),
    "infrastructure-hiccups": ("research", PRESETS["infrastructure-hiccups"]["plan"]),
    "duplicate-delivery": ("report", PRESETS["duplicate-delivery"]["plan"]),
    "hostile-tool-output": ("hostile", PRESETS["hostile-tool-output"]["plan"]),
}


async def run_arm(
    seeded: Any, name: str, kind: str, raw: dict[str, Any], policy: str, trials: int
) -> ChaosExperiment:
    wf = {"research": research_workflow, "report": research_report_workflow, "hostile": hostile_workflow}[
        kind
    ]()
    wf_id = await save_workflow(seeded, f"{name}-{policy}-{uuid.uuid4().hex[:6]}", wf)
    plan = ChaosPlan.model_validate(
        {
            **raw,
            "trials": trials,
            "max_affected_runs": trials,
            "recovery_policy": policy,
            "duration_seconds": 240,
        }
    )
    async with unit_of_work() as s:
        exp = await create_experiment(
            s,
            workspace_id=seeded.workspace.id,
            user_id=None,
            name=f"{name} [{policy}]",
            workflow_id=wf_id,
            plan=plan,
        )
        exp_id = exp.id
    with workers(3, 4, env=ENV, supervise=True) as pool:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            pool.tick()
            async with get_sessionmaker()() as s:
                e = await s.get(ChaosExperiment, exp_id)
                if e is not None and e.status != "running":
                    return e
            await asyncio.sleep(0.5)
    raise TimeoutError(name)


def row(scenario: str, policy: str, e: ChaosExperiment) -> dict[str, Any]:
    m = (e.results or {})["metrics"]
    r = e.results or {}
    return {
        "scenario": scenario,
        "recovery_policy": policy,
        "runs": r["planned_runs"],
        "injections": r["injections_total"],
        "completion": m["task_completion_rate"]["value"],
        "completed": m["task_completion_rate"]["numerator"],
        "exposure": m["fault_exposure"]["value"],
        "recovery_rate": m["recovery_rate"]["value"],
        "recovery_n": m["recovery_rate"]["denominator"],
        "attempts_per_step": m["retry_amplification"]["attempts_per_step"],
        "detect_median_ms": m["time_to_detection_ms"]["median"],
        "detect_n": m["time_to_detection_ms"]["n"],
        "recover_median_ms": m["time_to_recovery_ms"]["median"],
        "recover_p95_ms": m["time_to_recovery_ms"]["p95"],
        "recover_n": m["time_to_recovery_ms"]["n"],
        "dup_prevented": m["duplicate_side_effect_prevention"]["numerator"],
        "dup_total": m["duplicate_side_effect_prevention"]["denominator"],
        "dup_executions_found": m["duplicate_side_effect_prevention"][
            "duplicate_successful_executions_found"
        ],
        "consistency": m["state_consistency"]["value"],
        "consistency_checks": m["state_consistency"]["denominator"],
        "budget_compliance": m["budget_compliance"]["value"],
        "policy_held": m["policy_held_under_hostile_output"]["value"],
        "policy_runs": m["policy_held_under_hostile_output"]["denominator"],
        "status": e.status,
        "partial": r.get("partial"),
        "errors": ";".join(sorted({c["error"] for c in r["per_case"] if c["error"]})),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="chaos")
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    out = OUT / args.label
    out.mkdir(parents=True, exist_ok=True)
    await create_database()
    t0 = time.time()
    rows: list[dict[str, Any]] = []
    pair: dict[str, ChaosExperiment] = {}
    trials = 6 if args.quick else 12
    for name, (kind, raw) in SCENARIOS.items():
        for policy in POLICIES:
            seeded = await reset()
            await add_agent(
                seeded, "Gullible Agent", scenario="injection_follower", tools=["document_search"]
            )
            e = await run_arm(seeded, name.split(" ")[0], kind, raw, policy, trials)
            r = row(name, policy, e)
            print("chaos", r, flush=True)
            rows.append(r)
            pair[f"{name}|{policy}"] = e
    with (out / "scenarios.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    # Paired before/after (same plan, so the same fault schedule) for the scenarios where retries can matter.
    cmps = []
    from openeval.metrics import CaseRecord
    from openeval.stats import compare

    def records(e: ChaosExperiment) -> list[CaseRecord]:
        return [
            CaseRecord(case_id=str(c["case_index"]), attempt=0, passed=bool(c["completed"]))
            for c in (e.results or {})["per_case"]
        ]

    # recovery_policy is part of the plan hash, so the arms differ by design: compare the paired outcomes directly.
    for name in SCENARIOS:
        for lo, hi in (("no_retry", "as_configured"), ("as_configured", "extra_retries")):
            c = compare(records(pair[f"{name}|{lo}"]), records(pair[f"{name}|{hi}"]), min_n=min(10, trials))
            cmps.append(
                {
                    "scenario": name,
                    "comparison": f"{lo} -> {hi}",
                    "n": c.n_matched,
                    "no_retry": c.baseline_success,
                    "with_retry": c.candidate_success,
                    "diff": c.diff_mean,
                    "ci_low": c.ci[0] if c.ci else None,
                    "ci_high": c.ci[1] if c.ci else None,
                    "mcnemar_p": c.mcnemar_p,
                    "significant": c.significant,
                }
            )
    with (out / "retry_effect.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(cmps[0]))
        w.writeheader()
        w.writerows(cmps)
    _ = compare_experiments
    (out / "meta.json").write_text(
        json.dumps(
            {
                "label": args.label,
                "quick": args.quick,
                "trials_per_experiment": trials,
                "machine": machine_info(),
                "duration_s": round(time.time() - t0, 1),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "provider": "mock (deterministic); no model calls",
                "workers": "3 processes x concurrency 4, supervised",
                "lease_ttl_seconds": 3,
            },
            indent=2,
        )
    )
    await dispose_engine()
    print("results in", out)


if __name__ == "__main__":
    asyncio.run(main())
