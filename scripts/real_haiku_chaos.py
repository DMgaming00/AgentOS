"""Chaos Lab against the real-Claude-Haiku research workflow (needs live mode; see docs/ENABLE_REAL_CLAUDE.md).

    cd scripts && uv run python real_haiku_chaos.py

Faults are injected around real provider calls and real worker processes. Each experiment is 6 runs with a fixed seed.
Results go to benchmarks/results/real_haiku/chaos.json. Small n: read the paired intervals, not point estimates.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from autopilot_demo import Client, say, wait

TRIALS = int(os.environ.get("TRIALS", "6"))
OUT = (
    Path(__file__).resolve().parent.parent
    / "benchmarks"
    / "results"
    / "real_haiku"
    / ("chaos.json" if TRIALS == 6 else f"chaos_n{TRIALS}.json")
)
ARMS = [
    ("provider-brownout", "no_retry", "Haiku brownout, retries off"),
    ("provider-brownout", "as_configured", "Haiku brownout, retries on"),
    ("worker-crash-loop", "as_configured", "Haiku worker crashes, retries on"),
    ("worker-crash-loop", "extra_retries", "Haiku worker crashes, 5 attempts"),
]


def haiku_workflow(dev: Client) -> dict[str, Any]:
    """Three parallel single-fact lookups answered by the real Haiku agent from the hard-eval state, then an aggregation.
    (The seeded research-report workflow uses scripted agents after a default reseed, so it is not used here.)"""
    state = json.loads((OUT.parent / "hard_state.json").read_text())
    agent = state["agents"]["careful"]["name"]
    questions = [
        ("lookup_aurora", "What is the p95 latency of Aurora Serve in ms?"),
        ("lookup_brio", "What is the Uptime SLA of Brio Compute in percent?"),
        ("lookup_cedar", "What is the Number of regions of Cedar Infer?"),
    ]
    nodes = [
        {
            "key": k,
            "type": "agent_task",
            "agent": agent,
            "tools": ["document_search"],
            "input": {"task": q + ' Return only a JSON object {"answer": <value>}.'},
            "retry": {"max_attempts": 3, "backoff_base_seconds": 0.5, "backoff_max_seconds": 2},
        }
        for k, q in questions
    ]
    nodes.append({"key": "combine", "type": "aggregation", "depends_on": [k for k, _ in questions]})
    return dev.post(
        "/api/v1/workflows",
        {"name": f"haiku-chaos-{time.strftime('%H%M%S')}", "definition": {"nodes": nodes}},
    )


def main() -> None:
    dev = Client("developer@agentos.dev", "agentos-dev")
    wf = haiku_workflow(dev)
    out: list[dict[str, Any]] = []
    only = os.environ.get(
        "ONLY_POLICY"
    )  # e.g. ONLY_POLICY=extra_retries appends one arm to an existing result file
    if only and OUT.exists():
        out = json.loads(OUT.read_text())["experiments"]
    for scenario, policy, label in ARMS:
        if only and policy != only:
            continue
        overrides = {
            "trials": TRIALS,
            "max_affected_runs": TRIALS,
            "recovery_policy": policy,
            "duration_seconds": 300 if TRIALS <= 6 else 600,
        }
        exp = dev.post(
            "/api/v1/chaos/experiments",
            {
                "name": f"{label} {time.strftime('%H%M%S')}",
                "workflow_id": wf["id"],
                "scenario": scenario,
                "overrides": overrides,
            },
        )
        say(f"{label}: experiment {exp['id'][:8]} running")
        done = wait(
            lambda exp=exp: (
                (e := dev.get(f"/api/v1/chaos/experiments/{exp['id']}"))["status"] != "running" and e
            ),
            label,
            timeout=2400,
            interval=5,
        )
        r = done.get("results") or {}
        m = r.get("metrics", {})
        row = {
            "label": label,
            "scenario": scenario,
            "recovery_policy": policy,
            "experiment_id": exp["id"],
            "status": done["status"],
            "planned_runs": r.get("planned_runs"),
            "injections": r.get("injections_total"),
            "completion": m.get("task_completion_rate"),
            "state_consistency": m.get("state_consistency"),
            "retry_amplification": m.get("retry_amplification"),
            "recovery_rate": m.get("recovery_rate"),
        }
        out.append(row)
        say(f"  {done['status']}: completion {row['completion']}  injections {row['injections']}")
    OUT.write_text(json.dumps({"model": "claude-haiku-5-5", "experiments": out}, indent=2, default=str))
    say(f"wrote {OUT}")


if __name__ == "__main__":
    main()
