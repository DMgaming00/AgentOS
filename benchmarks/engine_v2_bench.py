"""Engine v2 benchmarks: scaling with 1/2/4/8 worker processes, static vs fan_out DAGs, priority aging,
and crash recovery inside nested workflows. Real worker processes, isolated database, every number derived
from rows the engine wrote (attempt timestamps, ready_at, queue_wait_ms) plus psutil samples.

    cd benchmarks && uv run --group bench python engine_v2_bench.py --label engine_v2 [--quick]
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from harness import create_database, machine_info, pct, reset, run_metrics, start_run, wait_runs, workers
from run import fanout

from agentos_core.db.models import WorkflowRun
from agentos_core.db.session import dispose_engine, get_sessionmaker

OUT = Path(__file__).parent / "results"
CALC = {"type": "tool_task", "tool": "calculator", "arguments": {"operation": "add", "values": [1, 2]}}


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if rows:
        with path.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)


def static_fan(n: int) -> dict[str, Any]:
    """root -> n independent calculator tasks -> sink (all nodes declared up front)."""
    mid = [f"n{i:02d}" for i in range(n)]
    return {
        "nodes": [
            {"key": "root", "type": "aggregation"},
            *[{"key": k, "depends_on": ["root"], **CALC} for k in mid],
            {"key": "sink", "type": "aggregation", "depends_on": mid},
        ],
        "budget": {"max_concurrency": 64},
    }


def dynamic_fan(n: int) -> dict[str, Any]:
    """root -> fan_out (expands to n children at run time) -> sink."""
    return {
        "nodes": [
            {"key": "root", "type": "aggregation"},
            {
                "key": "fan",
                "type": "fan_out",
                "depends_on": ["root"],
                "over": "input.items",
                "max_items": n,
                "template": CALC,
            },
            {"key": "sink", "type": "aggregation", "depends_on": ["fan"]},
        ],
        "budget": {"max_concurrency": 64},
    }


async def wait_live_workers(n: int, timeout: float = 60) -> None:
    """Methodology guard: start measuring only once ``n`` worker processes have registered."""
    from sqlalchemy import func, select

    from agentos_core.db.models import Worker

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        async with get_sessionmaker()() as s:
            alive = (
                await s.execute(select(func.count()).select_from(Worker).where(Worker.status == "active"))
            ).scalar_one()
        if alive >= n:
            return
        await asyncio.sleep(0.25)
    raise TimeoutError(f"only some of {n} workers registered")


async def measure(run_ids: list[uuid.UUID], pool: Any) -> dict[str, Any]:
    wall = await wait_runs(run_ids, pool)
    res = pool.resource_summary()
    m = await run_metrics(run_ids)
    attempts, steps = m.pop("attempts_list"), {s.id: s for s in m.pop("steps_list")}
    span = (
        max(a.finished_at for a in attempts if a.finished_at) - min(a.started_at for a in attempts)
    ).total_seconds()
    start_lat = [
        float(a.queue_wait_ms)
        for a in attempts
        if a.queue_wait_ms is not None and steps[a.step_id].node_key != "root"
    ]
    return {
        "steps": m["steps"],
        "attempts": m["attempts"],
        "succeeded": m["succeeded"],
        "failed": m["failed"],
        "processing_s": round(span, 3),
        "wall_s": round(wall, 3),
        "steps_per_s": round(m["steps"] / span, 1),
        "executions_per_s": round(m["attempts"] / span, 1),
        "step_start_latency_p50_ms": round(pct(start_lat, 0.5) or 0, 1),
        "step_start_latency_p95_ms": round(pct(start_lat, 0.95) or 0, 1),
        **res,
    }


SCALING_OVERRIDES: dict[str, int | None] = {"max_workers": None, "reps": None}


async def suite_scaling(quick: bool) -> list[dict[str, Any]]:
    """Repetitions are the OUTER loop (cells are interleaved across time), because a single repetition proved
    sensitive to ambient load: the same cell varied by more than 3x between runs in a pilot."""
    rows = []
    n_wf, reps = (20, 1) if quick else (40, 3)
    reps = SCALING_OVERRIDES["reps"] or reps
    worker_counts = [
        w for w in ((1, 2, 4) if quick else (1, 2, 4, 8)) if w <= (SCALING_OVERRIDES["max_workers"] or 8)
    ]
    for rep in range(reps):
        for n_workers in worker_counts:
            for size in (5, 20):
                seeded = await reset()
                definition = fanout(size, kind="aggregation")
                run_ids = [await start_run(seeded, definition, name="sc") for _ in range(n_wf)]
                with workers(n_workers, 8) as pool:
                    row = await measure(run_ids, pool)
                row = {
                    "workers": n_workers,
                    "concurrency": 8,
                    "dag_size": size,
                    "workflows": n_wf,
                    "rep": rep,
                    **row,
                }
                print("scaling", row, flush=True)
                rows.append(row)
    return rows


async def suite_fanout(quick: bool) -> list[dict[str, Any]]:
    rows = []
    n_wf, reps = (10, 2) if quick else (20, 3)
    for rep in range(reps):
        for kind, builder in (("static", static_fan), ("fan_out", dynamic_fan)):
            for n in (5, 18):
                seeded = await reset()
                run_ids = [
                    await start_run(seeded, builder(n), name=f"fo{kind}", run_input={"items": list(range(n))})
                    for _ in range(n_wf)
                ]
                with workers(2, 8) as pool:
                    row = await measure(run_ids, pool)
                row = {"kind": kind, "rep": rep, "parallel_tasks": n, "workflows": n_wf, **row}
                print("fanout", row, flush=True)
                rows.append(row)
    return rows


async def suite_priority(quick: bool) -> list[dict[str, Any]]:
    """Starvation protection under a *stream* of high-priority arrivals.

    A pilot with everything submitted at once showed no effect, and that is expected: runs in a pre-filled backlog
    all age together, so a priority-0 run never closes the gap to a priority-9 run submitted at the same moment.
    Aging helps against *later* arrivals, so the benchmark submits 6 low-priority runs at t=0 and then a steady
    stream of high-priority runs faster than one worker can serve them. Aging is accelerated (0.5 s per priority
    point instead of the default 30 s) so the effect is visible in a short benchmark.
    """
    rows = []
    n_low, seconds, rate_hz = (4, 5, 6) if quick else (6, 10, 8)
    chain = {
        "nodes": [
            {
                "key": f"s{i}",
                "type": "aggregation",
                "depends_on": [f"s{i - 1}"] if i else [],
                "inject": {"sleep_seconds": 0.2},
            }
            for i in range(4)
        ]
    }
    for label, aging in (("aging_off", 1e9), ("aging_0_5s", 0.5)):
        for rep in range(2 if quick else 3):
            seeded = await reset()
            low = [await start_run(seeded, chain, name="low", priority=0) for _ in range(n_low)]
            high: list[uuid.UUID] = []
            with workers(1, 4, env={"AGENTOS_PRIORITY_AGING_SECONDS": str(aging)}) as pool:
                t0 = time.perf_counter()
                while time.perf_counter() - t0 < seconds:  # a steady arrival stream, faster than service
                    high.append(await start_run(seeded, chain, name="high", priority=9))
                    await asyncio.sleep(1 / rate_hz)
                await wait_runs(low + high, pool, timeout=300)
                total = time.perf_counter() - t0
            async with get_sessionmaker()() as s:
                got = await select_runs(s, low + high)
            lat = lambda ids: sorted((got[i].completed_at - got[i].created_at).total_seconds() for i in ids)  # noqa: E731
            lo, hi = lat(low), lat(high)
            stream_end = max(got[i].created_at for i in high)
            row = {
                "config": label,
                "rep": rep,
                "high_runs": len(high),
                "low_runs": n_low,
                "total_s": round(total, 2),
                "low_latency_p50_s": round(pct(lo, 0.5) or 0, 2),
                "low_latency_max_s": round(lo[-1], 2),
                "high_latency_p50_s": round(pct(hi, 0.5) or 0, 2),
                "high_latency_p95_s": round(pct(hi, 0.95) or 0, 2),
                "low_done_before_stream_ended_pct": round(
                    100 * sum(1 for i in low if got[i].completed_at < stream_end) / n_low, 1
                ),
            }
            print("priority", row, flush=True)
            rows.append(row)
    return rows


async def select_runs(s: Any, ids: list[uuid.UUID]) -> dict[uuid.UUID, WorkflowRun]:
    from sqlalchemy import select

    return {r.id: r for r in (await s.execute(select(WorkflowRun).where(WorkflowRun.id.in_(ids)))).scalars()}


async def suite_nested_recovery(quick: bool) -> list[dict[str, Any]]:
    """A worker dies inside a child run: time until the child step is reclaimed and the parent completes."""
    from agentos_core.testing import save_workflow  # not in older engine versions (baseline comparison)

    rows = []
    n = 3 if quick else 6
    for ttl in (2.0, 5.0) if quick else (2.0, 5.0, 10.0):
        seeded = await reset()
        await save_workflow(
            seeded,
            "bench-child",
            {
                "nodes": [
                    {
                        "key": "victim",
                        "type": "aggregation",
                        "retry": {"max_attempts": 3, "backoff_base_seconds": 0, "backoff_max_seconds": 0},
                        "inject": {
                            "sleep_seconds": 0.5,
                            "crash_worker_on_attempt": 1,
                            "crash_after_seconds": 0.5,
                        },
                    }
                ]
            },
        )
        parent = {
            "nodes": [
                {"key": "go", "type": "sub_workflow", "workflow": "bench-child"},
                {"key": "after", "type": "aggregation", "depends_on": ["go"]},
            ]
        }
        env = {
            "AGENTOS_LEASE_TTL_SECONDS": str(ttl),
            "AGENTOS_HEARTBEAT_INTERVAL_SECONDS": str(ttl / 5),
            "AGENTOS_COORDINATOR_INTERVAL_SECONDS": "0.25",
        }
        with workers(2, 4, env=env, supervise=True) as pool:
            await wait_live_workers(2)
            await asyncio.sleep(1.0)
            for i in range(n):
                run_id = await start_run(seeded, parent, name=f"nr{i}")
                await wait_runs([run_id], pool, timeout=120)
                async with get_sessionmaker()() as s:
                    from sqlalchemy import select

                    child = (
                        await s.execute(select(WorkflowRun).where(WorkflowRun.parent_run_id == run_id))
                    ).scalar_one()
                    parent_run = await s.get(WorkflowRun, run_id)
                m = await run_metrics([child.id])
                victim = sorted(m["attempts_list"], key=lambda a: a.attempt_number)
                crash_at = victim[0].started_at + timedelta(seconds=1.0)
                row = {
                    "lease_ttl_s": ttl,
                    "run": i,
                    "parent_status": parent_run.status,
                    "child_status": child.status,
                    "victim_attempts": len(victim),
                    "detect_s": round((victim[0].finished_at - crash_at).total_seconds(), 3),
                    "parent_completed_after_crash_s": round(
                        (parent_run.completed_at - crash_at).total_seconds(), 3
                    ),
                    "duplicate_attempts": max(0, len(victim) - 2),
                }
                print("nested_recovery", row, flush=True)
                rows.append(row)
    return rows


SUITES = {
    "scaling": suite_scaling,
    "fanout": suite_fanout,
    "priority": suite_priority,
    "nested_recovery": suite_nested_recovery,
}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="engine_v2")
    ap.add_argument("--suites", default=",".join(SUITES))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--max-workers", type=int, help="scaling suite: largest worker count")
    ap.add_argument("--reps", type=int, help="scaling suite: repetitions")
    args = ap.parse_args()
    SCALING_OVERRIDES.update(max_workers=args.max_workers, reps=args.reps)
    out = OUT / args.label
    out.mkdir(parents=True, exist_ok=True)
    await create_database()
    t0 = time.time()
    for name in args.suites.split(","):
        write_csv(out / f"{name}.csv", await SUITES[name](args.quick))
    (out / "meta.json").write_text(
        json.dumps(
            {
                "label": args.label,
                "quick": args.quick,
                "machine": machine_info(),
                "duration_s": round(time.time() - t0, 1),
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "suites": args.suites.split(","),
                "provider": "none: no model calls (aggregation and calculator nodes only)",
            },
            indent=2,
        )
    )
    await dispose_engine()
    print("results in", out)


if __name__ == "__main__":
    asyncio.run(main())
