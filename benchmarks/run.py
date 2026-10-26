"""AgentOS benchmark suite.

    uv run --group bench python benchmarks/run.py --label baseline            # all suites
    uv run --group bench python benchmarks/run.py --label quick --suites throughput --quick

Requires PostgreSQL and Redis from docker compose. Uses its own database
(agentos_bench) and spawns real worker processes. Writes CSV + JSON to
benchmarks/results/<label>/. Charts: benchmarks/plot.py.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import statistics
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from harness import (
    create_database,
    machine_info,
    pct,
    reset,
    run_metrics,
    start_run,
    wait_runs,
    workers,
)

from agentos_core.db.session import dispose_engine
from agentos_core.providers.base import Message, ModelConfig
from agentos_core.providers.mock import MockProvider
from agentos_core.testing import add_agent

OUT = Path(__file__).parent / "results"


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def fanout(size: int, *, kind: str, agent: str | None = None, concurrency: int = 64) -> dict[str, Any]:
    """root -> (size-2) independent nodes -> sink."""

    def node(key: str, deps: list[str]) -> dict[str, Any]:
        if kind == "agent":
            return {"key": key, "type": "agent_task", "agent": agent, "tools": [], "depends_on": deps}
        return {"key": key, "type": "aggregation", "depends_on": deps}

    middle = [f"n{i:02d}" for i in range(size - 2)]
    return {
        "nodes": [node("root", []), *[node(k, ["root"]) for k in middle], node("sink", middle)],
        "budget": {"max_concurrency": concurrency, "max_model_calls": 1000, "max_cost_usd": 20},
    }


async def suite_throughput(quick: bool) -> list[dict[str, Any]]:
    rows = []
    n_workflows = 20 if quick else 40
    configs = [(w, c, s, "aggregation") for s in (5, 20) for w in (1, 2, 4) for c in (4, 8)]
    configs += [(w, 8, 5, "agent") for w in (1, 2, 4)]
    if quick:
        configs = [(1, 8, 5, "aggregation"), (2, 8, 5, "aggregation"), (4, 8, 5, "aggregation")]
    for n_workers, conc, size, kind in configs:
        seeded = await reset()
        if kind == "agent":
            await add_agent(seeded, "Bench Echo", scenario="echo", tools=[])
        definition = fanout(size, kind=kind, agent="Bench Echo")
        # Pre-fill the backlog, then start the workers: measures processing, not submission speed.
        run_ids = [await start_run(seeded, definition, name="tp") for _ in range(n_workflows)]
        with workers(n_workers, conc) as pool:
            wall = await wait_runs(run_ids, pool)
            res = pool.resource_summary()
        m = await run_metrics(run_ids)
        attempts = m.pop("attempts_list")
        steps = {s.id: s for s in m.pop("steps_list")}
        first_claim = min(a.started_at for a in attempts)
        last_done = max(a.finished_at for a in attempts if a.finished_at)
        span = (last_done - first_claim).total_seconds()
        exec_ms = [(a.finished_at - a.started_at).total_seconds() * 1000 for a in attempts if a.finished_at]
        # Start latency only for steps that became ready while workers were running (not the pre-filled roots).
        start_lat = [
            float(a.queue_wait_ms)
            for a in attempts
            if a.queue_wait_ms is not None and steps[a.step_id].node_key != "root"
        ]
        row = {
            "workers": n_workers,
            "concurrency": conc,
            "dag_size": size,
            "node_kind": kind,
            "workflows": n_workflows,
            "steps": m["steps"],
            "processing_s": round(span, 3),
            "wall_s": round(wall, 3),
            "workflows_per_min": round(n_workflows / span * 60, 1),
            "steps_per_s": round(m["steps"] / span, 1),
            "step_start_latency_p50_ms": round(pct(start_lat, 0.5) or 0, 1),
            "step_start_latency_p95_ms": round(pct(start_lat, 0.95) or 0, 1),
            "step_exec_overhead_p50_ms": round(pct(exec_ms, 0.5) or 0, 1),
            "step_exec_overhead_p95_ms": round(pct(exec_ms, 0.95) or 0, 1),
            "succeeded": m["succeeded"],
            **res,
        }
        print("throughput", row, flush=True)
        rows.append(row)
    return rows


async def suite_parallel(quick: bool) -> list[dict[str, Any]]:
    rows = []
    k, work = 8, 0.5
    reps = 2 if quick else 5
    for mode, max_conc in (("sequential", 1), ("parallel", k)):
        for rep in range(reps):
            seeded = await reset()
            nodes = [
                {"key": f"w{i}", "type": "aggregation", "inject": {"sleep_seconds": work}} for i in range(k)
            ]
            nodes.append({"key": "sink", "type": "aggregation", "depends_on": [f"w{i}" for i in range(k)]})
            definition = {"nodes": nodes, "budget": {"max_concurrency": max_conc}}
            with workers(2, 4) as pool:
                await asyncio.sleep(1.5)
                run_id = await start_run(seeded, definition, name=mode)
                await wait_runs([run_id], pool)
            m = await run_metrics([run_id])
            ideal = work * (k if mode == "sequential" else 1)
            row = {
                "mode": mode,
                "rep": rep,
                "nodes": k,
                "work_per_node_s": work,
                "max_concurrency": max_conc,
                "completion_s": round(m["makespan_s"], 3),
                "ideal_s": ideal,
                "scheduler_overhead_s": round(m["makespan_s"] - ideal, 3),
            }
            print("parallel", row, flush=True)
            rows.append(row)
    return rows


async def suite_recovery(quick: bool) -> list[dict[str, Any]]:
    rows = []
    ttls = (2.0, 5.0) if quick else (2.0, 5.0, 10.0)
    n = 3 if quick else 6
    sleep_s, crash_after = 0.5, 0.5
    for ttl in ttls:
        seeded = await reset()
        definition = {
            "nodes": [
                {"key": "before", "type": "aggregation"},
                {
                    "key": "victim",
                    "type": "aggregation",
                    "depends_on": ["before"],
                    "retry": {"max_attempts": 3, "backoff_base_seconds": 0, "backoff_max_seconds": 0},
                    "inject": {
                        "sleep_seconds": sleep_s,
                        "crash_worker_on_attempt": 1,
                        "crash_after_seconds": crash_after,
                    },
                },
                {"key": "after", "type": "aggregation", "depends_on": ["victim"]},
            ],
        }
        env = {
            "AGENTOS_LEASE_TTL_SECONDS": str(ttl),
            "AGENTOS_HEARTBEAT_INTERVAL_SECONDS": str(ttl / 5),
            "AGENTOS_COORDINATOR_INTERVAL_SECONDS": "0.25",
        }
        with workers(2, 4, env=env, supervise=True) as pool:
            await asyncio.sleep(1.5)
            for i in range(n):
                run_id = await start_run(seeded, definition, name=f"rec{i}")
                await wait_runs([run_id], pool, timeout=120)
                m = await run_metrics([run_id])
                steps = {s.id: s for s in m["steps_list"]}
                by_key = {s.node_key: s for s in steps.values()}
                victim = sorted(
                    (a for a in m["attempts_list"] if a.step_id == by_key["victim"].id),
                    key=lambda a: a.attempt_number,
                )
                crash_at = victim[0].started_at + timedelta(seconds=sleep_s + crash_after)
                detected = (
                    (victim[0].finished_at - crash_at).total_seconds() if victim[0].finished_at else None
                )
                reclaimed = (victim[1].started_at - crash_at).total_seconds() if len(victim) > 1 else None
                reexecuted = sum(1 for k in ("before", "after") if by_key[k].attempt_count > 1)
                row = {
                    "lease_ttl_s": ttl,
                    "heartbeat_s": ttl / 5,
                    "run": i,
                    "succeeded": m["succeeded"] == 1,
                    "victim_attempts": len(victim),
                    "first_attempt_status": victim[0].status,
                    "detect_s": round(detected, 3) if detected is not None else None,
                    "reclaim_s": round(reclaimed, 3) if reclaimed is not None else None,
                    "duplicate_attempts": max(0, len(victim) - 2),
                    "completed_step_reexecutions": reexecuted,
                    "worker_restarts_total": pool.restarts,
                }
                print("recovery", row, flush=True)
                rows.append(row)
    return rows


async def suite_overhead(quick: bool) -> list[dict[str, Any]]:
    rows = []
    n = 10 if quick else 30
    for latency_ms in (0, 100):
        # Direct provider calls: pure (simulated) inference + mock policy cost, no orchestration.
        p = MockProvider()
        msgs = [Message(role="user", text='<task_input>{"input": {"x": 1}}</task_input>')]
        cfg = ModelConfig(model="mock-echo", extra={"scenario": "echo", "latency_ms": latency_ms})
        t0 = time.perf_counter()
        for _ in range(n):
            await p.generate("s", msgs, [], cfg)
        direct_ms = (time.perf_counter() - t0) / n * 1000

        seeded = await reset()
        await add_agent(seeded, "Bench Echo", scenario="echo", tools=[], config={"latency_ms": latency_ms})
        definition = {"nodes": [{"key": "a", "type": "agent_task", "agent": "Bench Echo", "tools": []}]}
        with workers(1, 1) as pool:
            await asyncio.sleep(1.5)
            run_ids = []
            for _ in range(n):  # sequential: isolates per-step cost from contention
                rid = await start_run(seeded, definition, name="ovh")
                await wait_runs([rid], pool)
                run_ids.append(rid)
        m = await run_metrics(run_ids)
        attempts = m["attempts_list"]
        exec_ms = [(a.finished_at - a.started_at).total_seconds() * 1000 for a in attempts]
        e2e_ms = [float(x) for x in []]
        from sqlalchemy import select

        from agentos_core.db.models import WorkflowRun
        from agentos_core.db.session import get_sessionmaker

        async with get_sessionmaker()() as s:
            runs = (await s.execute(select(WorkflowRun).where(WorkflowRun.id.in_(run_ids)))).scalars().all()
        e2e_ms = [(r.completed_at - r.created_at).total_seconds() * 1000 for r in runs]
        row = {
            "provider_latency_ms": latency_ms,
            "samples": n,
            "direct_provider_call_ms": round(direct_ms, 2),
            "step_execution_p50_ms": round(statistics.median(exec_ms), 1),
            "step_execution_p95_ms": round(pct(exec_ms, 0.95) or 0, 1),
            "run_end_to_end_p50_ms": round(statistics.median(e2e_ms), 1),
            "run_end_to_end_p95_ms": round(pct(e2e_ms, 0.95) or 0, 1),
            "orchestration_overhead_p50_ms": round(statistics.median(exec_ms) - direct_ms, 1),
        }
        print("overhead", row, flush=True)
        rows.append(row)
    return rows


async def suite_ab(quick: bool) -> list[dict[str, Any]]:
    """Interleaved A/B of event batching on identical workloads (same code, same session)."""
    rows = []
    reps = 2 if quick else 4
    configs = [(4, 8, 20), (2, 8, 20), (2, 8, 5)]
    for rep in range(reps):
        for n_workers, conc, size in configs:
            for batching in (False, True):
                seeded = await reset()
                definition = fanout(size, kind="aggregation")
                run_ids = [await start_run(seeded, definition, name="ab") for _ in range(40)]
                with workers(n_workers, conc, env={"AGENTOS_EVENT_BATCHING": str(batching).lower()}) as pool:
                    await wait_runs(run_ids, pool)
                    res = pool.resource_summary()
                m = await run_metrics(run_ids)
                attempts = m.pop("attempts_list")
                m.pop("steps_list")
                span = (
                    max(a.finished_at for a in attempts if a.finished_at)
                    - min(a.started_at for a in attempts)
                ).total_seconds()
                exec_ms = [
                    (a.finished_at - a.started_at).total_seconds() * 1000 for a in attempts if a.finished_at
                ]
                row = {
                    "rep": rep,
                    "workers": n_workers,
                    "concurrency": conc,
                    "dag_size": size,
                    "event_batching": batching,
                    "steps": m["steps"],
                    "processing_s": round(span, 3),
                    "steps_per_s": round(m["steps"] / span, 1),
                    "step_exec_overhead_p50_ms": round(pct(exec_ms, 0.5) or 0, 1),
                    "step_exec_overhead_p95_ms": round(pct(exec_ms, 0.95) or 0, 1),
                    "lock_wait_share": res["lock_wait_share"],
                    "succeeded": m["succeeded"],
                }
                print("ab", row, flush=True)
                rows.append(row)
    return rows


SUITES = {
    "throughput": suite_throughput,
    "parallel": suite_parallel,
    "recovery": suite_recovery,
    "overhead": suite_overhead,
    "ab": suite_ab,
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--suites", default=",".join(SUITES))
    ap.add_argument("--quick", action="store_true")
    args = ap.parse_args()
    out = OUT / args.label
    out.mkdir(parents=True, exist_ok=True)
    await create_database()
    meta = {
        "label": args.label,
        "quick": args.quick,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": machine_info(),
        "suites": {},
    }
    for name in args.suites.split(","):
        t0 = time.perf_counter()
        rows = await SUITES[name](args.quick)
        write_csv(out / f"{name}.csv", rows)
        meta["suites"][name] = {"rows": len(rows), "seconds": round(time.perf_counter() - t0, 1)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    await dispose_engine()
    print(f"results in {out}")


if __name__ == "__main__":
    asyncio.run(main())

_ = uuid
