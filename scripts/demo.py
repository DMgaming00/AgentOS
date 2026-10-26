"""Reproducible demos driven entirely through the public API.

    uv run python scripts/demo.py research      # Demo A: multi-agent report with approval
    uv run python scripts/demo.py recovery      # Demo B: worker crash -> lease expiry -> recovery
    uv run python scripts/demo.py permissions   # Demo C: reject -> resubmit -> approve -> crash -> no duplicate
    uv run python scripts/demo.py all

Requires a running stack (docker compose up, or scripts/dev.sh) with at least two
workers for the crash demos. Approvals are decided by this script via the API;
pass --interactive to decide them yourself in the UI instead.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

import httpx2 as httpx

API = os.environ.get("AGENTOS_API_URL", "http://localhost:8000")
WEB = os.environ.get("AGENTOS_WEB_URL", "http://localhost:3000")


class Client:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=API, timeout=30)
        r = self.http.post(
            "/api/v1/auth/login", json={"email": "admin@agentos.dev", "password": "agentos-admin"}
        )
        r.raise_for_status()
        self.http.headers["Authorization"] = f"Bearer {r.json()['access_token']}"

    def get(self, path: str, **kw: Any) -> Any:
        r = self.http.get(path, **kw)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: Any = None, **kw: Any) -> Any:
        r = self.http.post(path, json=body or {}, **kw)
        if r.status_code >= 400:
            raise SystemExit(f"POST {path} -> {r.status_code}: {r.text}")
        return r.json()


def say(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def check(cond: bool, msg: str) -> None:
    print(f"   {'PASS' if cond else 'FAIL'}  {msg}", flush=True)
    if not cond:
        raise SystemExit(1)


def workflow_id(c: Client, name: str) -> str:
    for w in c.get("/api/v1/workflows")["items"]:
        if w["name"] == name:
            return str(w["id"])
    raise SystemExit(f"workflow {name!r} not found - run the seed (python -m agentos.seed)")


def wait(pred: Any, timeout: float = 90, interval: float = 0.5, what: str = "condition") -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = pred()
        if v:
            return v
        time.sleep(interval)
    raise SystemExit(f"timed out waiting for {what}")


def wait_status(c: Client, run_id: str, statuses: set[str], timeout: float = 120) -> dict[str, Any]:
    result: dict[str, Any] = wait(
        lambda: (r := c.get(f"/api/v1/runs/{run_id}"))["status"] in statuses and r,
        timeout,
        what=f"run status in {statuses}",
    )
    return result


def next_approval(
    c: Client, run_id: str, exclude: set[str] = frozenset(), timeout: float = 90
) -> dict[str, Any]:  # type: ignore[assignment]
    def find() -> Any:
        items = c.get("/api/v1/approvals", params={"status": "pending", "run_id": run_id})["items"]
        items = [a for a in items if a["id"] not in exclude]
        return items[0] if items else None

    result: dict[str, Any] = wait(find, timeout, what="pending approval")
    return result


def decide(c: Client, approval: dict[str, Any], decision: str, reason: str, interactive: bool) -> None:
    if interactive:
        say(f"Decide in the UI: {WEB}/approvals  (expected: {decision})")
        wait(
            lambda: c.get(f"/api/v1/approvals/{approval['id']}")["status"] != "pending",
            3600,
            1,
            "your decision",
        )
        return
    c.post(
        f"/api/v1/approvals/{approval['id']}/{decision}",
        {"reason": reason},
        headers={"Idempotency-Key": f"demo-{approval['id']}-{decision}"},
    )
    say(f"{'approved' if decision == 'approve' else 'rejected'} approval {approval['id'][:8]}: {reason!r}")


def steps_by_key(c: Client, run_id: str) -> dict[str, Any]:
    return {s["node_key"]: s for s in c.get(f"/api/v1/runs/{run_id}/steps")}


# ----------------------------------------------------------------------------- demos


def demo_research(c: Client, interactive: bool) -> None:
    say(
        "Demo A - multi-agent research report (research x3 in parallel -> analyze -> write -> approval -> export)"
    )
    run = c.post(
        f"/api/v1/workflows/{workflow_id(c, 'research-report')}/runs",
        headers={"Idempotency-Key": f"demo-a-{time.time()}"},
    )
    rid = run["id"]
    say(f"run {rid}  ->  {WEB}/runs/{rid}")
    ap = next_approval(c, rid)
    say(f"paused for approval: {ap['requested_action']} (agent: {ap['requesting_agent']})")
    check(not c.get(f"/api/v1/runs/{rid}/artifacts"), "nothing written before approval")
    decide(c, ap, "approve", "report reviewed", interactive)
    final = wait_status(c, rid, {"succeeded", "failed"})
    check(final["status"] == "succeeded", f"run succeeded in {final['duration_ms']} ms")
    steps = steps_by_key(c, rid)
    research = [s for k, s in steps.items() if k.startswith("research_")]
    starts = [s["attempts"][0]["started_at"] for s in research]
    ends = [s["attempts"][0]["finished_at"] for s in research]
    check(max(starts) < min(ends), "the three research steps overlapped in time (parallel)")
    arts = c.get(f"/api/v1/runs/{rid}/artifacts")
    check(
        len(arts) == 1,
        f"artifact {arts[0]['filename']} ({arts[0]['byte_size']} bytes, sha256 {arts[0]['sha256'][:12]}...)",
    )
    usage = c.get(f"/api/v1/runs/{rid}/usage")
    check(
        all(u["input_tokens"] is not None for u in usage),
        f"{len(usage)} model calls recorded, estimated cost ${float(final['cost_spent_usd']):.4f} "
        f"({'simulated' if usage and usage[0]['cost_basis'] == 'simulated' else usage[0]['cost_basis'] if usage else '-'})",
    )


def demo_recovery(c: Client, interactive: bool) -> None:
    say("Demo B - worker failure recovery (fetch -> process A + B -> aggregate -> export)")
    say(
        "process_a's first attempt kills its worker process (os._exit(137)) mid-step; another worker must recover it"
    )
    run = c.post(
        f"/api/v1/workflows/{workflow_id(c, 'failure-recovery')}/runs",
        headers={"Idempotency-Key": f"demo-b-{time.time()}"},
    )
    rid = run["id"]
    say(f"run {rid}  ->  {WEB}/runs/{rid}")

    def crashed() -> Any:
        s = steps_by_key(c, rid)["process_a"]
        return s if any(a["status"] == "lease_expired" for a in s["attempts"]) else None

    t0 = time.time()
    step = wait(crashed, 60, what="lease expiry of the crashed attempt")
    lost = next(a for a in step["attempts"] if a["status"] == "lease_expired")
    say(
        f"lease of {lost['worker_id']} expired; detected {time.time() - t0:.1f}s after the demo started waiting"
    )
    ap = next_approval(c, rid)
    decide(c, ap, "approve", "export recovered results", interactive)
    final = wait_status(c, rid, {"succeeded", "failed"})
    check(final["status"] == "succeeded", "run succeeded after recovery")
    steps = steps_by_key(c, rid)
    a = steps["process_a"]["attempts"]
    check(
        [x["status"] for x in a] == ["lease_expired", "succeeded"],
        f"process_a attempts: {[(x['worker_id'], x['status']) for x in a]}",
    )
    check(a[0]["worker_id"] != a[1]["worker_id"], "a different worker completed the retry")
    check(
        steps["fetch_input"]["attempt_count"] == 1,
        "fetch_input (completed before the crash) was not re-executed",
    )
    check(
        steps["process_b"]["attempt_count"] == 1, "process_b (completed before the crash) was not re-executed"
    )
    check(len(c.get(f"/api/v1/runs/{rid}/artifacts")) == 1, "exactly one exported artifact")


def demo_permissions(c: Client, interactive: bool) -> None:
    say(
        "Demo C - permission enforcement: reject -> no side effect -> resubmission -> approve -> idempotent export"
    )
    run = c.post(
        f"/api/v1/workflows/{workflow_id(c, 'approval-workflow')}/runs",
        headers={"Idempotency-Key": f"demo-c-{time.time()}"},
    )
    rid = run["id"]
    say(f"run {rid}  ->  {WEB}/runs/{rid}")
    first = next_approval(c, rid)
    decide(c, first, "reject", "please add a revision note", interactive)
    time.sleep(1)
    check(not c.get(f"/api/v1/runs/{rid}/artifacts"), "rejected action was not executed (no artifact)")
    second = next_approval(c, rid, exclude={first["id"]})
    check(
        second["tool_execution_id"] != first["tool_execution_id"],
        "the agent resubmitted a new, separately approved action",
    )
    reused = c.http.post(f"/api/v1/approvals/{first['id']}/approve", json={})
    check(reused.status_code == 409, "a decided approval cannot be reused (HTTP 409)")
    decide(c, second, "approve", "revision accepted", interactive)
    say("the writer's worker crashes right after the approved write; the retry must not write again")
    final = wait_status(c, rid, {"succeeded", "failed"})
    check(final["status"] == "succeeded", "run succeeded")
    arts = c.get(f"/api/v1/runs/{rid}/artifacts")
    check(len(arts) == 1, f"exactly one artifact: {arts[0]['filename']}")
    tes = c.get(f"/api/v1/runs/{rid}/tool-executions")
    writes = [t["status"] for t in tes if t["tool_name"] == "artifact_writer" and t["status"] != "denied"]
    check(writes == ["rejected", "succeeded"], f"artifact_writer executions: {writes}")
    draft = steps_by_key(c, rid)["draft_note"]
    check(
        draft["output"]["artifact"].get("idempotent_replay") is True,
        "retry replayed the recorded result (idempotency key)",
    )
    check(len(draft["attempts"]) >= 4, f"draft_note attempts: {[x['status'] for x in draft['attempts']]}")
    denied = [t for t in tes if t["status"] == "denied"]
    check(
        len(denied) == 1 and denied[0]["tool_name"] == "artifact_writer",
        "Untrusted Agent's call to a tool it is not allowed to use was denied by policy",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("demo", choices=["research", "recovery", "permissions", "all"])
    parser.add_argument("--interactive", action="store_true", help="decide approvals in the web UI")
    args = parser.parse_args()
    c = Client()
    workers = [
        w for w in c.get("/api/v1/metrics/observability", params={"days": 1})["workers"] if w["healthy"]
    ]
    say(f"{len(workers)} healthy worker(s): {', '.join(w['id'] for w in workers)}")
    if args.demo in ("recovery", "permissions", "all") and len(workers) < 2:
        print(
            "note: crash demos need a second worker (or a supervisor that restarts the crashed one)",
            file=sys.stderr,
        )
    for name in ["research", "recovery", "permissions"] if args.demo == "all" else [args.demo]:
        {"research": demo_research, "recovery": demo_recovery, "permissions": demo_permissions}[name](
            c, args.interactive
        )
        print()
    say("all checks passed")


if __name__ == "__main__":
    main()
