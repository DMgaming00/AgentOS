"""One real-model Autopilot cycle on Claude Haiku (needs live mode, a key and a spend cap; see docs/ENABLE_REAL_CLAUDE.md).

    cd scripts && uv run python real_haiku_autopilot.py

The extraction agent, the Failure Intelligence diagnosis and the optimizer all use Claude Haiku. Nothing is promoted or
deployed: the script stops after the paired experiments, and writes whatever happened (including "no failures to fix")
to benchmarks/results/real_haiku/autopilot.json. No result is assumed in advance.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from autopilot_demo import Client, datasets, evaluate, experiment, say

from agentos_core.autopilot.demo import FLAWED_INSTRUCTIONS

MODEL = "claude-haiku-5-5"

_post = Client.post


def _confirmed_post(self: Client, path: str, body: Any = None, **kw: Any) -> Any:
    """The user authorized this paid run (spend cap in .env); acknowledge the worst-case cost prompt for evaluations."""
    if isinstance(body, dict) and "confirm_cost" in body:
        body = {**body, "confirm_cost": True}
    return _post(self, path, body, **kw)


Client.post = _confirmed_post  # type: ignore[method-assign]
OUT = Path(__file__).resolve().parent.parent / "benchmarks" / "results" / "real_haiku" / "autopilot.json"


def main() -> None:
    admin = Client("admin@agentos.dev", "agentos-admin")
    dev = Client("developer@agentos.dev", "agentos-dev")
    result: dict[str, Any] = {"model": MODEL, "started": time.strftime("%Y-%m-%dT%H:%M:%S%z")}

    policy = admin.get("/api/v1/autopilot/policy")
    policy["diagnosis_provider"] = "anthropic"
    policy["optimizer_provider"] = "anthropic"
    if not any(m["model"] == MODEL for m in policy["allowed_models"]):
        policy["allowed_models"].append({"provider": "anthropic", "model": MODEL})
    admin.req("PUT", "/api/v1/autopilot/policy", policy)
    say("policy: diagnosis and optimizer use Anthropic")

    agent = dev.post(
        "/api/v1/agents",
        {
            "name": f"Extraction Agent (Haiku) {time.strftime('%m%d-%H%M%S')}",
            "description": "Real-model run of the Autopilot cycle",
            "system_instructions": FLAWED_INSTRUCTIONS,
            "provider": "anthropic",
            "model": MODEL,
            "model_configuration": {},
            "allowed_tool_ids": ["document_search", "structured_data_reader"],
            "max_steps": 6,
            "max_cost_usd": "0.5",
            "timeout_seconds": 180,
        },
    )
    result["agent_id"] = agent["id"]
    ds = datasets(dev)
    ev = evaluate(dev, agent, ds["development"])
    m = ev["metrics"]["task_success_rate"]
    result["baseline_development"] = {
        "passed": m["numerator"],
        "cases": m["denominator"],
        "value": m["value"],
    }
    say(f"baseline on development: {m['numerator']}/{m['denominator']} cases pass (real Haiku)")

    found = dev.post("/api/v1/autopilot/failures/analyze", {"agent_id": agent["id"], "window_hours": 2})
    result["failure_categories"] = found["by_category"]
    say(f"failure categories: {found['by_category']}")
    cats = {k: v for k, v in found["by_category"].items() if v}
    if not cats:
        result["note"] = "no failures on the development split, so there was nothing to diagnose or repair"
        OUT.write_text(json.dumps(result, indent=2))
        say(result["note"])
        return

    category = max(cats, key=lambda k: cats[k])
    d = dev.post("/api/v1/autopilot/diagnoses", {"agent_id": agent["id"], "category": category})
    result["diagnosis"] = {
        k: d.get(k) for k in ("status", "confidence", "hypothesis", "evidence_refs", "category")
    }
    say(f"diagnosis ({d['status']}): {d.get('hypothesis')}")

    cands = dev.post(
        "/api/v1/autopilot/candidates/generate", {"agent_id": agent["id"], "diagnosis_id": d["id"]}
    )
    result["candidates"] = [
        {"title": c["title"], "status": c["status"], "violations": c.get("policy_violations")} for c in cands
    ]
    result["experiments"] = []
    for c in cands:
        say(f"candidate {c['title']!r}: {c['status']}")
        if c["status"] in ("rejected_policy", "invalid"):
            continue
        exp = experiment(dev, agent, c, ds["validation"])
        r = exp["results"]
        result["experiments"].append(
            {
                "candidate": c["title"],
                "decision": exp["decision"],
                "summary": exp["eligibility"]["summary"],
                "baseline_success": r["baseline"]["metrics"]["task_success_rate"]["value"],
                "candidate_success": r["candidate"]["metrics"]["task_success_rate"]["value"],
                "diff_mean": r["comparison"]["diff_mean"],
                "ci95": r["comparison"]["ci95"],
                "n_matched": r["comparison"]["n_matched"],
            }
        )
        say(f"  decision: {exp['decision']}: {exp['eligibility']['summary']}")
    result["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    OUT.write_text(json.dumps(result, indent=2))
    say(f"wrote {OUT}")


if __name__ == "__main__":
    main()
