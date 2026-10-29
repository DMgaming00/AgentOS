"""A paired Autopilot experiment on real Claude Haiku where the candidate change is a prompt edit (needs live mode).

    cd scripts && uv run python real_haiku_paired.py

Baseline: the Arena "Bare" Haiku agent (search tool, minimal instructions). Candidate: the same agent with the "Careful"
instructions (JSON numbers, citations, abstain rather than guess), authored by hand and validated by the same policy as any
optimizer output. Evaluated on the arena-extraction validation split (16 cases) with 3 trials, interleaved, through the
same gates. Nothing is promoted. Results: benchmarks/results/real_haiku/paired.json.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from autopilot_demo import Client, say, wait

from agentos_core.arena.contenders import BASE, CAREFUL, FORMAT

MODEL = "claude-haiku-5-5"
OUT = Path(__file__).resolve().parent.parent / "benchmarks" / "results" / "real_haiku" / "paired.json"
_post = Client.post


def _confirmed(self: Client, path: str, body: Any = None, **kw: Any) -> Any:
    """The spend is authorized by the .env cap; acknowledge the worst-case cost prompt for evaluations."""
    if isinstance(body, dict) and "confirm_cost" in body:
        body = {**body, "confirm_cost": True}
    return _post(self, path, body, **kw)


Client.post = _confirmed  # type: ignore[method-assign]


def main() -> None:
    admin = Client("admin@agentos.dev", "agentos-admin")
    dev = Client("developer@agentos.dev", "agentos-dev")
    policy = admin.get("/api/v1/autopilot/policy")
    if not any(m["model"] == MODEL for m in policy["allowed_models"]):
        policy["allowed_models"].append({"provider": "anthropic", "model": MODEL})
    policy["max_eval_trials"] = 3
    admin.req("PUT", "/api/v1/autopilot/policy", policy)

    agent = dev.post(
        "/api/v1/agents",
        {
            "name": f"Extraction Bare (Haiku) {time.strftime('%m%d-%H%M%S')}",
            "description": "Baseline for the real-model paired experiment",
            "system_instructions": BASE,
            "provider": "anthropic",
            "model": MODEL,
            "model_configuration": {},
            "allowed_tool_ids": ["document_search"],
            "max_steps": 6,
            "max_cost_usd": "0.5",
            "timeout_seconds": 180,
        },
    )
    cand = dev.post(
        "/api/v1/autopilot/candidates",
        {
            "agent_id": agent["id"],
            "title": "Careful instructions: JSON numbers, citations, abstain rather than guess",
            "changes": [
                {
                    "field": "system_instructions",
                    "value": BASE + FORMAT + CAREFUL,
                    "reason": "The Arena shows the Careful configuration answering more extraction and grounded questions correctly.",
                }
            ],
            "expected_benefits": ["fewer wrong or ungrounded answers"],
            "risks": ["more abstentions"],
        },
    )
    say(f"candidate {cand['title']!r}: {cand['status']} {cand.get('policy_violations') or ''}")
    ds = next(
        d
        for d in dev.get("/api/v1/evaluations/datasets")["items"]
        if d["name"] == "arena-extraction" and d["split"] == "validation"
    )
    exp = dev.post(
        "/api/v1/experiments",
        {
            "agent_id": agent["id"],
            "dataset_id": ds["id"],
            "candidate_id": cand["id"],
            "trials": 3,
            "confirm_cost": True,
        },
    )
    say(f"experiment {exp['id'][:8]} running (16 cases x 3 trials x 2 arms)")
    done = wait(
        lambda: (e := dev.get(f"/api/v1/experiments/{exp['id']}"))["status"] != "running" and e,
        "experiment",
        timeout=1500,
        interval=5,
    )
    r = done["results"]
    out = {
        "model": MODEL,
        "experiment_id": exp["id"],
        "decision": done["decision"],
        "summary": done["eligibility"]["summary"],
        "baseline_success": r["baseline"]["metrics"]["task_success_rate"],
        "candidate_success": r["candidate"]["metrics"]["task_success_rate"],
        "comparison": r["comparison"],
        "trials": 3,
        "cases": ds["case_count"],
        "candidate": cand["title"],
    }
    OUT.write_text(json.dumps(out, indent=2, default=str))
    say(f"decision: {out['decision']}: {out['summary']}")
    say(
        f"success {out['baseline_success']['value']:.3f} -> {out['candidate_success']['value']:.3f}; comparison {r['comparison']}"
    )


if __name__ == "__main__":
    main()
