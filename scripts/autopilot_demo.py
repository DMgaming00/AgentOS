"""Autopilot demos, driven entirely through the public API against a running stack.

    uv run python scripts/autopilot_demo.py repair      # Demo 1: detect and repair an agent failure
    uv run python scripts/autopilot_demo.py fake        # Demo 2: reject a cheaper-but-worse candidate
    uv run python scripts/autopilot_demo.py rollback    # Demo 3: live regression -> rollback (Autopilot Lab)
    uv run python scripts/autopilot_demo.py all

Each run creates a fresh copy of the deliberately flawed Extraction Agent (versions, experiments and
deployments are immutable, so demos never reuse or delete earlier records). All models are the
deterministic mock provider unless you seed real providers; results are labeled simulated.
Approvals are decided by this script with the admin account; use --interactive to decide in the UI.
"""

from __future__ import annotations

import argparse
import os
import time
from typing import Any

import httpx2 as httpx

from agentos_core.autopilot.demo import FLAWED_INSTRUCTIONS, live_extraction_workflow

API = os.environ.get("AGENTOS_API_URL", "http://localhost:8000")
WEB = os.environ.get("AGENTOS_WEB_URL", "http://localhost:3000")


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(cond: bool, msg: str) -> None:
    print(f"   {'PASS' if cond else 'FAIL'}  {msg}", flush=True)
    if not cond:
        raise SystemExit(1)


class Client:
    def __init__(self, email: str, password: str, workspace: str | None = None) -> None:
        self.http = httpx.Client(base_url=API, timeout=60)
        r = self.http.post("/api/v1/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        data = r.json()
        self.http.headers["Authorization"] = f"Bearer {data['access_token']}"
        if workspace:
            ws = next(m for m in data["memberships"] if m["workspace_name"] == workspace)
            self.http.headers["X-Workspace-Id"] = ws["workspace_id"]
        self.workspace = workspace

    def req(self, method: str, path: str, body: Any = None, expect: int | None = None, **kw: Any) -> Any:
        r = self.http.request(method, path, json=body, **kw)
        if expect is not None:
            return r
        if r.status_code >= 400:
            raise SystemExit(f"{method} {path} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    def get(self, path: str, **kw: Any) -> Any:
        return self.req("GET", path, **kw)

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        key = f"demo-{time.time_ns()}"
        return self.req("POST", path, body, headers={"Idempotency-Key": key}, **kw)


def wait(fn: Any, what: str, timeout: float = 240, interval: float = 1.0) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = fn()
        if v:
            return v
        time.sleep(interval)
    raise SystemExit(f"timed out waiting for {what}")


def datasets(c: Client) -> dict[str, dict[str, Any]]:
    return {
        d["split"]: d
        for d in c.get("/api/v1/evaluations/datasets")["items"]
        if d["name"] == "numeric-extraction"
    }


def fresh_agent(dev: Client) -> dict[str, Any]:
    name = f"Extraction Agent {time.strftime('%m%d-%H%M%S')}"
    agent = dev.post(
        "/api/v1/agents",
        {
            "name": name,
            "description": "Fresh flawed copy for the Autopilot demo",
            "system_instructions": FLAWED_INSTRUCTIONS,
            "provider": "mock",
            "model": "mock-extractor",
            "model_configuration": {"scenario": "extractor"},
            "allowed_tool_ids": ["document_search", "structured_data_reader"],
            "max_steps": 6,
            "max_cost_usd": "0.5",
            "timeout_seconds": 120,
        },
    )
    say(f"created agent {name!r} (v1, flawed: formats prices as currency strings)")
    return agent


def evaluate(
    dev: Client, agent: dict[str, Any], dataset: dict[str, Any], version_id: str | None = None
) -> dict[str, Any]:
    run = dev.post(
        "/api/v1/evaluations/run",
        {
            "agent_id": agent["id"],
            "dataset_id": dataset["id"],
            "agent_version_id": version_id,
            "trials": 1,
            "confirm_cost": False,
        },
    )
    return wait(
        lambda: (r := dev.get(f"/api/v1/evaluations/runs/{run['id']}"))["status"] == "completed" and r,
        "evaluation run",
    )


def experiment(
    dev: Client, agent: dict[str, Any], candidate: dict[str, Any], dataset: dict[str, Any]
) -> dict[str, Any]:
    e = dev.post(
        "/api/v1/experiments",
        {
            "agent_id": agent["id"],
            "dataset_id": dataset["id"],
            "candidate_id": candidate["id"],
            "trials": 1,
            "confirm_cost": False,
        },
    )
    say(
        f"experiment {e['id'][:8]} running: v{e['baseline_version']} vs v{e['candidate_version']} on "
        f"{dataset['split']} ({dataset['case_count']} cases)  ->  {WEB}/autopilot/experiments/{e['id']}"
    )
    return wait(
        lambda: (r := dev.get(f"/api/v1/experiments/{e['id']}"))["status"] != "running" and r, "experiment"
    )


def approve(admin: Client, dev: Client, exp: dict[str, Any], interactive: bool) -> dict[str, Any]:
    req = dev.post(
        "/api/v1/deployments/promote",
        {
            "experiment_id": exp["id"],
            "mode": "full",
            "canary_percent": 0,
            "reason": "eligible in controlled experiment",
        },
    )
    say(f"promotion requested ({req['id'][:8]}); an administrator must approve")
    if interactive:
        say(f"approve it in the UI: {WEB}/autopilot/deployments")
        return wait(
            lambda: (
                (
                    r := next(
                        (x for x in dev.get("/api/v1/deployments/requests")["items"] if x["id"] == req["id"]),
                        None,
                    )
                )
                and r["status"] != "pending"
                and r
            ),
            "your decision",
            timeout=3600,
        )
    viewer_try = dev.req("POST", f"/api/v1/deployments/requests/{req['id']}/approve", {}, expect=403)
    check(viewer_try.status_code == 403, "a developer cannot approve the deployment (admin required)")
    return admin.post(f"/api/v1/deployments/requests/{req['id']}/approve", {"reason": "evidence reviewed"})


def live_runs(dev: Client, agent: dict[str, Any], n: int) -> list[dict[str, Any]]:
    wf_def = live_extraction_workflow()
    wf_def["nodes"][0]["agent"] = agent["name"]
    wfs = {w["name"]: w for w in dev.get("/api/v1/workflows")["items"]}
    name = f"live-{agent['id'][:8]}"
    wf = wfs.get(name) or dev.post(
        "/api/v1/workflows", {"name": name, "description": "live extraction", "definition": wf_def}
    )
    runs = [dev.post(f"/api/v1/workflows/{wf['id']}/runs", {"input": {}}) for _ in range(n)]
    done = []
    for r in runs:
        done.append(
            wait(
                lambda r=r: (
                    (x := dev.get(f"/api/v1/runs/{r['id']}"))["status"] in ("succeeded", "failed") and x
                ),
                "live run",
                timeout=60,
            )
        )
    return done


def pinned_version(dev: Client, run_id: str) -> str:
    return dev.get(f"/api/v1/runs/{run_id}/steps")[0]["agent_version_id"]


# ----------------------------------------------------------------------------- demos


def demo_repair(admin: Client, dev: Client, interactive: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    say("Demo 1 - detect and repair an agent failure")
    ds = datasets(dev)
    agent = fresh_agent(dev)
    v1 = agent["active_version_id"]
    ev = evaluate(dev, agent, ds["development"])
    m = ev["metrics"]["task_success_rate"]
    check(
        0 < m["value"] < 1,
        f"benchmark run on development split: {m['numerator']}/{m['denominator']} cases pass",
    )
    found = dev.post("/api/v1/autopilot/failures/analyze", {"agent_id": agent["id"], "window_hours": 2})
    check(
        found["by_category"].get("OUTPUT_SCHEMA_INVALID", 0) > 0,
        f"Failure Intelligence classified failures: {found['by_category']}",
    )
    d = dev.post(
        "/api/v1/autopilot/diagnoses", {"agent_id": agent["id"], "category": "OUTPUT_SCHEMA_INVALID"}
    )
    check(
        d["status"] == "hypothesis" and len(d["evidence_refs"]) > 0,
        f"diagnosis (hypothesis, {d['confidence']} confidence, {len(d['evidence_refs'])} evidence refs): {d['hypothesis']}",
    )
    cands = dev.post(
        "/api/v1/autopilot/candidates/generate", {"agent_id": agent["id"], "diagnosis_id": d["id"]}
    )
    for c in cands:
        say(
            f"  candidate {c['title']!r}: {c['status']}"
            + (f" ({c['policy_violations'][0]})" if c["policy_violations"] else "")
        )
    check(
        any(c["status"] == "rejected_policy" for c in cands),
        "out-of-policy candidate (adds a tool) rejected in code",
    )
    fix = next(c for c in cands if c["title"].startswith("Return numeric"))
    exp = experiment(dev, agent, fix, ds["validation"])
    r = exp["results"]
    check(exp["decision"] == "eligible", f"decision: {exp['eligibility']['summary']}")
    say(
        f"  success {r['baseline']['metrics']['task_success_rate']['value']:.1%} -> "
        f"{r['candidate']['metrics']['task_success_rate']['value']:.1%}, paired diff {r['comparison']['diff_mean']:+.3f} "
        f"CI95 {r['comparison']['ci95']}, n={r['comparison']['n_matched']} (simulated mock provider)"
    )
    check(
        approve(admin, dev, exp, interactive)["status"] == "approved", "administrator approved the promotion"
    )
    agent_now = dev.get(f"/api/v1/agents/{agent['id']}")
    check(
        agent_now["active_version_id"] == fix["candidate_version_id"],
        f"active version is now v{fix['candidate_version']}",
    )
    run = live_runs(dev, agent_now, 1)[0]
    check(
        run["status"] == "succeeded" and pinned_version(dev, run["id"]) == fix["candidate_version_id"],
        "a new live run uses the promoted version and succeeds",
    )
    test = evaluate(dev, agent_now, ds["test"])
    say(
        f"  held-out test split (never used for selection): {test['metrics']['task_success_rate']['numerator']}/"
        f"{test['metrics']['task_success_rate']['denominator']} pass; v1 baseline id {v1[:8]}"
    )
    return agent_now, d


def demo_fake(admin: Client, dev: Client, interactive: bool) -> None:
    say("Demo 2 - reject a fake improvement (cheaper because it skips retrieval)")
    ds = datasets(dev)
    agent = fresh_agent(dev)
    evaluate(dev, agent, ds["development"])
    dev.post("/api/v1/autopilot/failures/analyze", {"agent_id": agent["id"], "window_hours": 2})
    d = dev.post(
        "/api/v1/autopilot/diagnoses", {"agent_id": agent["id"], "category": "OUTPUT_SCHEMA_INVALID"}
    )
    cands = dev.post(
        "/api/v1/autopilot/candidates/generate", {"agent_id": agent["id"], "diagnosis_id": d["id"]}
    )
    fast = next(c for c in cands if c["title"].startswith("Fast path"))
    exp = experiment(dev, agent, fast, ds["validation"])
    r = exp["results"]
    check(
        r["cost"]["candidate_total"] < r["cost"]["baseline_total"],
        f"candidate is cheaper: total ${r['cost']['candidate_total']:.5f} vs ${r['cost']['baseline_total']:.5f}",
    )
    check(
        r["candidate"]["metrics"]["task_success_rate"]["value"]
        < r["baseline"]["metrics"]["task_success_rate"]["value"],
        f"but task success fell {r['baseline']['metrics']['task_success_rate']['value']:.1%} -> "
        f"{r['candidate']['metrics']['task_success_rate']['value']:.1%}",
    )
    check(
        len(r["comparison"]["regressions"]) > 0,
        f"{len(r['comparison']['regressions'])} per-case regressions detected",
    )
    check(exp["decision"] == "not_eligible", f"decision: {exp['eligibility']['summary']}")
    resp = dev.req("POST", "/api/v1/deployments/promote", {"experiment_id": exp["id"]}, expect=409)
    check(resp.status_code == 409, "promotion request refused: " + resp.json()["error"]["message"])


def demo_rollback(interactive: bool) -> None:
    say("Demo 3 - live regression and rollback in the Autopilot Lab (test environment)")
    admin = Client("admin@agentos.dev", "agentos-admin", "Autopilot Lab")
    dev = Client("developer@agentos.dev", "agentos-dev", "Autopilot Lab")
    ds = datasets(dev)
    agent = fresh_agent(dev)
    # 1. a healthy version: the numeric-format fix
    fix = dev.post(
        "/api/v1/autopilot/candidates",
        {
            "agent_id": agent["id"],
            "title": "Return numeric values as JSON numbers",
            "changes": [
                {
                    "field": "system_instructions",
                    "value": FLAWED_INSTRUCTIONS
                    + " Return every numeric value as a plain JSON number with no units or currency symbols.",
                    "reason": "fix numeric formatting",
                }
            ],
        },
    )
    approve(admin, dev, experiment(dev, agent, fix, ds["validation"]), interactive)
    healthy = live_runs(dev, agent, 5)
    check(
        all(r["status"] == "succeeded" for r in healthy),
        f"v{fix['candidate_version']} live runs: 5/5 succeeded",
    )
    # 2. a "cost cap" that passes offline validation (single-subject cases) but breaks live two-subject tasks
    cap = dev.post(
        "/api/v1/autopilot/candidates",
        {
            "agent_id": agent["id"],
            "title": "Cap agent steps at 2",
            "changes": [{"field": "max_steps", "value": 2, "reason": "reduce worst-case cost"}],
        },
    )
    exp = experiment(dev, agent, cap, ds["validation"])
    check(
        exp["decision"] == "eligible" and not exp["eligibility"]["improvement_demonstrated"],
        "offline validation: eligible (non-inferior), no improvement claimed - the validation set does not cover multi-subject tasks",
    )
    approve(admin, dev, exp, interactive)
    regressed = live_runs(dev, agent, 5)
    check(
        all(r["status"] == "failed" for r in regressed),
        f"v{cap['candidate_version']} live runs: 5/5 failed (step limit)",
    )
    hist = wait(
        lambda: next(
            (
                d
                for d in dev.get("/api/v1/deployments/history", params={"agent_id": agent["id"]})["items"]
                if any(e["type"] == "regression_detected" for e in d["rollout_events"])
            ),
            None,
        ),
        "regression detection",
        timeout=60,
    )
    ev = next(e for e in hist["rollout_events"] if e["type"] == "regression_detected")["payload"]
    check(
        hist["version"] == cap["candidate_version"],
        f"monitoring flagged deployment of v{hist['version']}: failure rate {ev['previous']['failure_rate']:.0%} -> "
        f"{ev['current']['failure_rate']:.0%} (n={ev['current']['n']})",
    )
    resp = dev.req(
        "POST",
        "/api/v1/deployments/rollback",
        {"agent_id": agent["id"], "reason": "developer attempt"},
        expect=403,
    )
    check(resp.status_code == 403, "a developer cannot roll back (admin required)")
    if interactive:
        say(f"roll back in the UI: {WEB}/autopilot/deployments (Autopilot Lab workspace)")
        wait(
            lambda: (
                dev.get(f"/api/v1/agents/{agent['id']}")["active_version_id"] == fix["candidate_version_id"]
            ),
            "rollback",
            timeout=3600,
        )
    else:
        rb = admin.post(
            "/api/v1/deployments/rollback",
            {"agent_id": agent["id"], "reason": "live failure-rate regression"},
        )
        check(rb["version"] == fix["candidate_version"], f"rolled back to v{rb['version']} by admin")
    after = live_runs(dev, agent, 1)[0]
    check(
        after["status"] == "succeeded" and pinned_version(dev, after["id"]) == fix["candidate_version_id"],
        "new runs use the restored version and succeed",
    )
    check(
        all(pinned_version(dev, r["id"]) == cap["candidate_version_id"] for r in regressed),
        "runs made during the regression stay pinned to the version they ran",
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("demo", choices=["repair", "fake", "rollback", "all"])
    ap.add_argument("--interactive", action="store_true")
    args = ap.parse_args()
    admin = Client("admin@agentos.dev", "agentos-admin", "Demo Workspace")
    dev = Client("developer@agentos.dev", "agentos-dev", "Demo Workspace")
    t0 = time.time()
    if args.demo in ("repair", "all"):
        demo_repair(admin, dev, args.interactive)
        print()
    if args.demo in ("fake", "all"):
        demo_fake(admin, dev, args.interactive)
        print()
    if args.demo in ("rollback", "all"):
        demo_rollback(args.interactive)
        print()
    say(f"all checks passed in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
