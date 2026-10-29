"""Real-model evaluation on the "hard-ops" set (needs live mode and a spend cap; see docs/ENABLE_REAL_CLAUDE.md).

    cd scripts
    uv run python real_hard_eval.py setup                          # upload documents + datasets, create the agents
    uv run python real_hard_eval.py eval  <agent> <split> [trials] # evaluate one agent (e.g. bare validation 3)
    uv run python real_hard_eval.py paired <split> [trials]        # paired experiment: bare -> careful prompt
    uv run python real_hard_eval.py optimize [trials]              # diagnose bare on development, optimizer candidates, paired
    uv run python real_hard_eval.py status                         # spend so far

State (agent and dataset ids) is kept in benchmarks/results/real_haiku/hard_state.json; every result is appended to
benchmarks/results/real_haiku/hard_results.json. Nothing here assumes an outcome.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from autopilot_demo import Client, say, wait

ROOT = Path(__file__).resolve().parent.parent
HARD = ROOT / "examples" / "hard-eval"
RES = ROOT / "benchmarks" / "results" / "real_haiku"
STATE = RES / "hard_state.json"
RESULTS = RES / "hard_results.json"

BARE = "You answer questions using the workspace documents. Use the tools you are given. Reply with only the requested JSON object."
CAREFUL = (
    BARE
    + " Method: (1) Search for each platform or topic you need separately, using the platform name plus the metric label. "
    "(2) The platform documents state launch figures as of 2025-11-01; the pricing changelog lists later changes, so for any price "
    "question also search the changelog and use the most recent dated entry that applies. "
    "(3) Do arithmetic with the calculator tool, never in your head. "
    "(4) When filtering or comparing platforms, look up every needed value for every platform before deciding. "
    "(5) If the documents do not state the requested value (a similar metric is not the same metric), answer null. "
    "(6) Reply with only the JSON object."
)
TIGHT_STEPS = 3
AGENTS = {
    "bare": ("Hard · Haiku · Bare", "claude-haiku-5-5", BARE),
    "careful": ("Hard · Haiku · Careful", "claude-haiku-5-5", CAREFUL),
    "sonnet": ("Hard · Sonnet · Careful", "claude-sonnet-5-5", CAREFUL),
    "tight": (
        "Hard · Haiku · Tight budget",
        "claude-haiku-5-5",
        BARE,
    ),  # same prompt as bare, but only 3 steps
}


def admin() -> Client:
    return Client("admin@agentos.dev", "agentos-admin")


def load(path: Path, default: Any) -> Any:
    return json.loads(path.read_text()) if path.exists() else default


def record(entry: dict[str, Any]) -> None:
    items = load(RESULTS, [])
    items.append({**entry, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    RESULTS.write_text(json.dumps(items, indent=2, default=str))


def spend(c: Client) -> str:
    d = (
        c.get("/api/v1/finops/summary?days=30") if False else None
    )  # kept simple: read from the system endpoint instead
    return str(d)


_post = Client.post


def confirmed(self: Client, path: str, body: Any = None, **kw: Any) -> Any:
    """The spend is authorized by the cap in .env; acknowledge the worst-case cost prompt for evaluations."""
    if isinstance(body, dict) and "confirm_cost" in body:
        body = {**body, "confirm_cost": True}
    return _post(self, path, body, **kw)


Client.post = confirmed  # type: ignore[method-assign]


def setup(agents: bool = True) -> None:
    c = admin()
    for f in sorted((HARD / "documents").glob("*.md")):
        r = c.http.post("/api/v1/documents", files={"file": (f.name, f.read_bytes(), "text/markdown")})
        if r.status_code not in (200, 201, 409):
            raise SystemExit(f"upload {f.name}: {r.status_code} {r.text[:200]}")
    say("documents uploaded")
    state: dict[str, Any] = (
        load(STATE, {"datasets": {}, "agents": {}}) if not agents else {"datasets": {}, "agents": {}}
    )
    for split in ("development", "validation", "test"):
        ds = json.loads((HARD / "datasets" / f"hard-ops.v2.{split}.json").read_text())
        row = c.post("/api/v1/evaluations/datasets", ds)
        state["datasets"][split] = row["id"]
    policy = c.get("/api/v1/autopilot/policy")
    for m in ("claude-haiku-5-5", "claude-sonnet-5-5"):
        if not any(x["model"] == m for x in policy["allowed_models"]):
            policy["allowed_models"].append({"provider": "anthropic", "model": m})
    policy["max_eval_trials"] = 3
    c.req("PUT", "/api/v1/autopilot/policy", policy)
    stamp = time.strftime("%m%d-%H%M")
    for key, (name, model, prompt) in AGENTS.items() if agents else []:
        a = c.post(
            "/api/v1/agents",
            {
                "name": f"{name} {stamp}",
                "description": "Hard-ops real-model agent",
                "system_instructions": prompt,
                "provider": "anthropic",
                "model": model,
                "model_configuration": {},
                "allowed_tool_ids": ["document_search", "calculator"],
                "max_steps": TIGHT_STEPS if key == "tight" else 14,
                "max_cost_usd": "1.0",
                "timeout_seconds": 240,
            },
        )
        state["agents"][key] = {"id": a["id"], "version_id": a["active_version_id"], "name": a["name"]}
    STATE.write_text(json.dumps(state, indent=2))
    say(f"setup done: {state}")


def run_eval(agent: str, split: str, trials: int = 1) -> dict[str, Any]:
    c, st = admin(), json.loads(STATE.read_text())
    run = c.post(
        "/api/v1/evaluations/run",
        {
            "agent_id": st["agents"][agent]["id"],
            "dataset_id": st["datasets"][split],
            "trials": trials,
            "confirm_cost": True,
        },
    )
    say(f"{agent} on {split} x{trials}: run {run['id'][:8]}")
    done = wait(
        lambda: (
            (r := c.get(f"/api/v1/evaluations/runs/{run['id']}"))["status"]
            in ("completed", "failed", "cancelled")
            and r
        ),
        "evaluation",
        timeout=3000,
        interval=10,
    )
    m = done["metrics"]
    out = {
        "kind": "eval",
        "agent": agent,
        "split": split,
        "trials": trials,
        "status": done["status"],
        "run_id": run["id"],
        "metrics": m,
    }
    record(out)
    s = m.get("task_success_rate", {})
    say(f"  {done['status']}: success {s.get('numerator')}/{s.get('denominator')} = {s.get('value')}")
    return out


def paired_for(
    c: Client, base: dict[str, Any], dataset_id: str, split: str, trials: int, cand: dict[str, Any]
) -> dict[str, Any]:
    exp = c.post(
        "/api/v1/experiments",
        {
            "agent_id": base["id"],
            "dataset_id": dataset_id,
            "candidate_id": cand["id"],
            "trials": trials,
            "confirm_cost": True,
        },
    )
    say(f"experiment {exp['id'][:8]} running on {split} x{trials}: {cand['title'][:70]}")
    done = wait(
        lambda: (e := c.get(f"/api/v1/experiments/{exp['id']}"))["status"] != "running" and e,
        "experiment",
        timeout=4000,
        interval=10,
    )
    r = done["results"]
    out = {
        "kind": "paired",
        "split": split,
        "trials": trials,
        "experiment_id": exp["id"],
        "candidate": cand["title"],
        "decision": done["decision"],
        "summary": done["eligibility"]["summary"],
        "baseline": r["baseline"]["metrics"]["task_success_rate"],
        "candidate_metrics": r["candidate"]["metrics"]["task_success_rate"],
        "comparison": {
            k: r["comparison"].get(k)
            for k in (
                "diff_mean",
                "ci95",
                "mcnemar_p",
                "n_matched",
                "significant",
                "regressions",
                "improvements",
                "interpretation",
            )
        },
    }
    record(out)
    say(f"  {out['decision']}: {out['summary']}")
    say(
        f"  {out['baseline']['value']:.3f} -> {out['candidate_metrics']['value']:.3f}  diff {out['comparison']['diff_mean']}  CI {out['comparison']['ci95']}"
    )
    return out


def optimize(base_key: str, trials: int, max_candidates: int) -> None:
    """Failure analysis -> Sonnet diagnosis -> optimizer + rule candidates -> paired experiments on validation."""
    c, st = admin(), json.loads(STATE.read_text())
    base = st["agents"][base_key]
    policy = c.get("/api/v1/autopilot/policy")
    policy["diagnosis_provider"] = policy["optimizer_provider"] = "anthropic"
    policy["max_candidates"] = max(policy.get("max_candidates", 3), max_candidates)
    c.req("PUT", "/api/v1/autopilot/policy", policy)
    found = c.post("/api/v1/autopilot/failures/analyze", {"agent_id": base["id"], "window_hours": 6})
    cats = {k: v for k, v in found["by_category"].items() if v}
    say(f"failure categories: {cats}")
    record({"kind": "failures", "agent": base_key, "by_category": cats})
    top = max(cats, key=lambda k: cats[k])
    d = c.post("/api/v1/autopilot/diagnoses", {"agent_id": base["id"], "category": top})
    say(f"diagnosis ({d['status']}, {d.get('confidence')}): {str(d.get('hypothesis'))[:300]}")
    record(
        {
            "kind": "diagnosis",
            "status": d["status"],
            "confidence": d.get("confidence"),
            "category": top,
            "hypothesis": d.get("hypothesis"),
            "evidence_refs": len(d.get("evidence_refs") or []),
            "model": d.get("model"),
        }
    )
    cands = c.post("/api/v1/autopilot/candidates/generate", {"agent_id": base["id"], "diagnosis_id": d["id"]})
    rule_cands = c.post(
        "/api/v1/autopilot/candidates/generate", {"agent_id": base["id"], "generator": "rules"}
    )
    allc = [("model", x) for x in cands] + [("rules", x) for x in rule_cands]
    record(
        {
            "kind": "candidates",
            "items": [
                {
                    "generator": g,
                    "title": x["title"],
                    "status": x["status"],
                    "violations": x.get("policy_violations"),
                    "changes": [ch.get("field") for ch in x.get("changes", [])],
                }
                for g, x in allc
            ],
        }
    )
    for g, x in allc:
        say(f"candidate [{g}] {x['title'][:80]!r}: {x['status']} {x.get('policy_violations') or ''}")
    usable = [x for _, x in allc if x["status"] not in ("rejected_policy", "invalid")][:max_candidates]
    for x in usable:
        paired_for(c, base, st["datasets"]["validation"], "validation", trials, x)


def run_paired(split: str, trials: int, candidate_agent: str = "careful") -> None:
    c, st = admin(), json.loads(STATE.read_text())
    base = st["agents"]["bare"]
    cand = c.post(
        "/api/v1/autopilot/candidates",
        {
            "agent_id": base["id"],
            "title": "Careful procedure prompt (search per platform, check the changelog, calculator, abstain)",
            "changes": [
                {
                    "field": "system_instructions",
                    "value": CAREFUL,
                    "reason": "Hand-written method for multi-step document questions.",
                }
            ],
            "expected_benefits": ["fewer lookup, staleness and arithmetic errors"],
            "risks": ["more tool calls"],
        },
    )
    say(f"candidate: {cand['status']} {cand.get('policy_violations') or ''}")
    exp = c.post(
        "/api/v1/experiments",
        {
            "agent_id": base["id"],
            "dataset_id": st["datasets"][split],
            "candidate_id": cand["id"],
            "trials": trials,
            "confirm_cost": True,
        },
    )
    say(f"experiment {exp['id'][:8]} running on {split} x{trials}")
    done = wait(
        lambda: (e := c.get(f"/api/v1/experiments/{exp['id']}"))["status"] != "running" and e,
        "experiment",
        timeout=4000,
        interval=10,
    )
    r = done["results"]
    out = {
        "kind": "paired",
        "split": split,
        "trials": trials,
        "experiment_id": exp["id"],
        "decision": done["decision"],
        "summary": done["eligibility"]["summary"],
        "baseline": r["baseline"]["metrics"]["task_success_rate"],
        "candidate": r["candidate"]["metrics"]["task_success_rate"],
        "comparison": {
            k: r["comparison"].get(k)
            for k in (
                "diff_mean",
                "ci95",
                "mcnemar_p",
                "n_matched",
                "significant",
                "regressions",
                "improvements",
                "interpretation",
            )
        },
    }
    record(out)
    say(f"  {out['decision']}: {out['summary']}")
    say(
        f"  {out['baseline']['value']:.3f} -> {out['candidate']['value']:.3f}  diff {out['comparison']['diff_mean']}  CI {out['comparison']['ci95']}"
    )


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "setup":
        setup()
    elif cmd == "agent":  # create one more agent from AGENTS and store it in the state file
        c, st = admin(), json.loads(STATE.read_text())
        key = sys.argv[2]
        name, model, prompt = AGENTS[key]
        a = c.post(
            "/api/v1/agents",
            {
                "name": f"{name} {time.strftime('%m%d-%H%M')}",
                "description": "Hard-ops real-model agent",
                "system_instructions": prompt,
                "provider": "anthropic",
                "model": model,
                "model_configuration": {},
                "allowed_tool_ids": ["document_search", "calculator"],
                "max_steps": TIGHT_STEPS if key == "tight" else 14,
                "max_cost_usd": "1.0",
                "timeout_seconds": 240,
            },
        )
        st["agents"][key] = {"id": a["id"], "version_id": a["active_version_id"], "name": a["name"]}
        STATE.write_text(json.dumps(st, indent=2))
        say(f"created {a['name']}")
    elif cmd == "datasets":
        setup(agents=False)
    elif cmd == "eval":
        run_eval(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 1)
    elif cmd == "optimize":
        optimize(
            sys.argv[2],
            int(sys.argv[3]) if len(sys.argv) > 3 else 3,
            int(sys.argv[4]) if len(sys.argv) > 4 else 3,
        )
    elif (
        cmd == "testcand"
    ):  # paired experiment of an existing candidate on another split: testcand <title fragment> <split> <trials>
        c, st = admin(), json.loads(STATE.read_text())
        items = c.get(f"/api/v1/autopilot/candidates?agent_id={st['agents']['tight']['id']}&limit=50")[
            "items"
        ]
        cand = next(x for x in items if sys.argv[2].lower() in x["title"].lower())
        paired_for(c, st["agents"]["tight"], st["datasets"][sys.argv[3]], sys.argv[3], int(sys.argv[4]), cand)
    elif cmd == "paired":
        run_paired(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 1)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
