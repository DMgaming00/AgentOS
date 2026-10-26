"""Generate the results tables in docs/benchmarks.md from results/*/ CSVs (no hand-copied numbers)."""

from __future__ import annotations

import csv
import json
import statistics as st
from pathlib import Path

RES = Path(__file__).parent / "results"


def rows(label: str, name: str) -> list[dict[str, str]]:
    p = RES / label / f"{name}.csv"
    return list(csv.DictReader(p.open())) if p.exists() else []


def table(headers: list[str], data: list[list[object]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---:" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in data]
    return "\n".join(out)


def main() -> str:
    meta = json.loads((RES / "optimized" / "meta.json").read_text())
    m = meta["machine"]
    parts = [
        f"Machine: {m['cpu']}, {m['cpu_count']} cores, {m['memory_gb']} GB RAM, {m['platform']}, "
        f"Python {m['python']}, {m['postgres']} in Docker Desktop. Run started {meta['started_at']}."
    ]

    base = {
        (r["workers"], r["concurrency"], r["dag_size"], r["node_kind"]): r
        for r in rows("baseline", "throughput")
    }
    data = []
    for r in rows("optimized", "throughput"):
        b = base.get((r["workers"], r["concurrency"], r["dag_size"], r["node_kind"]))
        data.append(
            [
                r["node_kind"],
                r["dag_size"],
                r["workers"],
                r["concurrency"],
                b["steps_per_s"] if b else "-",
                r["steps_per_s"],
                r["workflows_per_min"],
                r["step_start_latency_p50_ms"],
                r["step_start_latency_p95_ms"],
                r["step_exec_overhead_p50_ms"],
                r["step_exec_overhead_p95_ms"],
                r["lock_wait_share"],
                r["cpu_pct_mean"],
                r["rss_mb_max"],
            ]
        )
    parts.append(
        "### Benchmark 1 - scheduling throughput (40 workflows, pre-filled backlog, single trial)\n\n"
        + table(
            [
                "nodes",
                "DAG size",
                "workers",
                "conc.",
                "steps/s (baseline)",
                "steps/s",
                "workflows/min",
                "start p50 ms",
                "start p95 ms",
                "exec p50 ms",
                "exec p95 ms",
                "lock-wait share",
                "worker CPU % (sum)",
                "worker RSS MB (sum)",
            ],
            data,
        )
    )

    ab = rows("ab", "ab")
    data = []
    for cfg in sorted({(r["workers"], r["dag_size"]) for r in ab}, key=lambda c: (int(c[1]), int(c[0]))):

        def stats(mode: str, key: str, cfg: tuple[str, str] = cfg) -> list[float]:
            return [
                float(r[key])
                for r in ab
                if (r["workers"], r["dag_size"]) == cfg and r["event_batching"] == mode
            ]

        a, b = stats("False", "steps_per_s"), stats("True", "steps_per_s")
        data.append(
            [
                cfg[0],
                cfg[1],
                len(a),
                f"{st.median(a):.1f} ({min(a)}-{max(a)})",
                f"{st.median(b):.1f} ({min(b)}-{max(b)})",
                f"{100 * (st.median(b) - st.median(a)) / st.median(a):+.1f}%",
                f"{st.median(stats('False', 'step_exec_overhead_p50_ms')):.1f} -> {st.median(stats('True', 'step_exec_overhead_p50_ms')):.1f}",
            ]
        )
    parts.append(
        "### Optimization A/B - event batching (interleaved repetitions, same code and session)\n\n"
        + table(
            [
                "workers",
                "DAG size",
                "reps",
                "per-event writes: steps/s median (min-max)",
                "batched: steps/s median (min-max)",
                "median change",
                "exec p50 ms",
            ],
            data,
        )
    )

    par = rows("optimized", "parallel")
    data = []
    for mode in ("sequential", "parallel"):
        xs = [float(r["completion_s"]) for r in par if r["mode"] == mode]
        ideal = float(next(r["ideal_s"] for r in par if r["mode"] == mode))
        data.append(
            [
                mode,
                len(xs),
                f"{st.median(xs):.3f}",
                f"{min(xs):.3f}-{max(xs):.3f}",
                ideal,
                f"{st.median(xs) - ideal:.3f}",
            ]
        )
    seq = st.median(float(r["completion_s"]) for r in par if r["mode"] == "sequential")
    pl = st.median(float(r["completion_s"]) for r in par if r["mode"] == "parallel")
    parts.append(
        "### Benchmark 2 - parallel vs sequential (8 independent nodes x 0.5 s + sink)\n\n"
        + table(["mode", "reps", "completion s (median)", "range", "ideal s", "overhead s"], data)
        + f"\n\nMeasured speedup: **{seq / pl:.2f}x** (ideal 8x; the gap is per-step scheduling overhead, see Benchmark 1)."
    )

    rec = rows("optimized", "recovery")
    data = []
    for t in sorted({r["lease_ttl_s"] for r in rec}, key=float):
        rr = [r for r in rec if r["lease_ttl_s"] == t]
        det = [float(r["detect_s"]) for r in rr]
        rcl = [float(r["reclaim_s"]) for r in rr]
        data.append(
            [
                t,
                rr[0]["heartbeat_s"],
                len(rr),
                f"{sum(r['succeeded'] == 'True' for r in rr)}/{len(rr)}",
                f"{st.median(det):.2f} ({min(det):.2f}-{max(det):.2f})",
                f"{st.median(rcl):.2f} ({min(rcl):.2f}-{max(rcl):.2f})",
                sum(int(r["duplicate_attempts"]) for r in rr),
                sum(int(r["completed_step_reexecutions"]) for r in rr),
            ]
        )
    parts.append(
        "### Benchmark 3 - failure recovery (worker process killed mid-step)\n\n"
        + table(
            [
                "lease TTL s",
                "heartbeat s",
                "crashes",
                "recovered",
                "detection s median (range)",
                "reclaim s median (range)",
                "duplicate attempts",
                "completed-step re-executions",
            ],
            data,
        )
    )

    ov = rows("optimized", "overhead")
    data = [
        [
            r["provider_latency_ms"],
            r["samples"],
            r["direct_provider_call_ms"],
            r["step_execution_p50_ms"],
            r["step_execution_p95_ms"],
            r["run_end_to_end_p50_ms"],
            r["orchestration_overhead_p50_ms"],
        ]
        for r in ov
    ]
    parts.append(
        "### Benchmark 4 - orchestration overhead (single-call agent step, mock provider)\n\n"
        + table(
            [
                "mock latency ms",
                "samples",
                "direct provider call ms",
                "step exec p50 ms",
                "step exec p95 ms",
                "run end-to-end p50 ms",
                "AgentOS overhead p50 ms",
            ],
            data,
        )
    )
    return "\n\n".join(parts)


if __name__ == "__main__":
    print(main())
