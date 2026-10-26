"""Charts and a report fragment for the engine v2 benchmarks, generated from results/engine_v2/*.csv.

uv run --group bench python benchmarks/engine_v2_report.py     # then: python benchmarks/assemble_v2_report.py
"""

from __future__ import annotations

import csv
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

from plot import INK, MUTED, S1, S2, S3, plt, save

RES = Path(__file__).parent / "results" / "engine_v2"
FRAG = Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "fragments"


def load(name: str) -> list[dict[str, str]]:
    return list(csv.DictReader((RES / f"{name}.csv").open()))


def table(headers: list[str], rows: list[list[object]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def f(row: dict[str, str], key: str) -> float:
    return float(row[key])


def cells(rows: list[dict[str, str]], key: str) -> dict[tuple[int, int], list[float]]:
    out: dict[tuple[int, int], list[float]] = defaultdict(list)
    for r in rows:
        out[(int(r["workers"]), int(r["dag_size"]))].append(f(r, key))
    return out


def scaling_chart(rows: list[dict[str, str]]) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    workers = sorted({int(r["workers"]) for r in rows})
    for ax, key, ylabel, title in (
        (axes[0], "executions_per_s", "step executions per second", "Throughput (median, bars = min-max)"),
        (
            axes[1],
            "step_start_latency_p95_ms",
            "step start latency p95 (ms)",
            "Queueing delay (ready to started)",
        ),
    ):
        data = cells(rows, key)
        for size, color in ((5, S1), (20, S2)):
            med = [st.median(data[(w, size)]) for w in workers]
            lo = [min(data[(w, size)]) for w in workers]
            hi = [max(data[(w, size)]) for w in workers]
            ax.plot(
                workers,
                med,
                color=color,
                lw=2,
                marker="o",
                ms=8,
                mec="white",
                mew=2,
                label=f"{size}-node DAG",
            )
            ax.vlines(workers, lo, hi, color=color, lw=1.5, alpha=0.6)
        ax.set_xscale("log", base=2)
        ax.set_xticks(workers, [str(w) for w in workers])
        ax.set_xlabel("worker processes (concurrency 8 each)")
        ax.set_ylabel(ylabel)
        ax.set_ylim(bottom=0)
        ax.legend(loc="best")
        ax.set_title(title, fontsize=10, color=INK, loc="left")
    save(fig, "engine_v2_scaling")


def fanout_chart(rows: list[dict[str, str]]) -> dict[tuple[str, int], list[float]]:
    data: dict[tuple[str, int], list[float]] = defaultdict(list)
    for r in rows:
        data[(r["kind"], int(r["parallel_tasks"]))].append(f(r, "processing_s"))
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    sizes = sorted({k[1] for k in data})
    w = 0.36
    for off, kind, color, label in (
        (-w / 2 - 0.01, "static", S1, "static DAG (declared up front)"),
        (w / 2 + 0.01, "fan_out", S2, "fan_out node (expanded at run time)"),
    ):
        means = [st.fmean(data[(kind, n)]) for n in sizes]
        bars = ax.bar([i + off for i in range(len(sizes))], means, w, color=color, label=label)
        for b, v in zip(bars, means, strict=True):
            ax.annotate(
                f"{v:.1f}s",
                (b.get_x() + b.get_width() / 2, v),
                textcoords="offset points",
                xytext=(0, 3),
                ha="center",
                fontsize=9,
                color=INK,
            )
        for i, n in enumerate(sizes):  # min-max across repetitions
            vs = data[(kind, n)]
            ax.plot([i + off] * 2, [min(vs), max(vs)], color=MUTED, lw=1.5)
    ax.set_xticks(range(len(sizes)), [f"{n} parallel tasks" for n in sizes])
    ax.set_ylabel("processing time, seconds (20 workflows)")
    ax.set_ylim(0, max(max(v) for v in data.values()) * 1.3)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("Static vs fan_out: same work, 2 workers", fontsize=10, color=INK, loc="left")
    save(fig, "engine_v2_fanout")
    return data


def priority_chart(rows: list[dict[str, str]]) -> None:
    fig, ax = plt.subplots(figsize=(6.5, 3.4))
    configs = ["aging_off", "aging_0_5s"]
    labels = {"aging_off": "aging off", "aging_0_5s": "aging 0.5 s per point"}
    w = 0.36
    for off, key, color, label in (
        (-w / 2 - 0.01, "low_latency_p50_s", S1, "low priority (0), median"),
        (w / 2 + 0.01, "high_latency_p50_s", S2, "high priority (9), median"),
    ):
        vals = [st.fmean(f(r, key) for r in rows if r["config"] == c) for c in configs]
        bars = ax.bar([i + off for i in range(2)], vals, w, color=color, label=label)
        for b, v in zip(bars, vals, strict=True):
            ax.annotate(
                f"{v:.1f}s",
                (b.get_x() + b.get_width() / 2, v),
                textcoords="offset points",
                xytext=(0, 3),
                ha="center",
                fontsize=9,
                color=INK,
            )
    ax.set_xticks(range(2), [labels[c] for c in configs])
    ax.set_ylabel("run latency (submission to completion), seconds")
    ax.set_ylim(0, ax.get_ylim()[1] * 1.2)
    ax.legend(loc="upper left", fontsize=8)
    ax.set_title("Starvation protection under a high-priority stream", fontsize=10, color=INK, loc="left")
    save(fig, "engine_v2_priority")


def recovery_chart(rows: list[dict[str, str]]) -> None:
    fig, ax = plt.subplots(figsize=(5.5, 3.2))
    ttls = sorted({f(r, "lease_ttl_s") for r in rows})
    for t in ttls:
        ys = [f(r, "detect_s") for r in rows if f(r, "lease_ttl_s") == t]
        ax.plot([t] * len(ys), ys, "o", color=S1, ms=7, mec="white", mew=1.5, alpha=0.85)
        ax.plot([t], [st.median(ys)], "_", color=INK, ms=22, mew=2)
    ax.plot(ttls, ttls, color=MUTED, lw=1, ls="--")
    ax.annotate(
        "detection = lease TTL",
        (ttls[-1], ttls[-1]),
        textcoords="offset points",
        xytext=(-110, 6),
        fontsize=8,
        color=MUTED,
    )
    ax.set_xticks(ttls, [f"{t:g}" for t in ttls])
    ax.set_xlabel("lease TTL (s)")
    ax.set_ylabel("crash → lease expiry detected (s)")
    ax.set_title("Worker killed inside a child run", fontsize=10, color=INK, loc="left")
    ax.set_ylim(bottom=0)
    save(fig, "engine_v2_nested_recovery")


def scaling_rows(rows: list[dict[str, str]]) -> list[list[object]]:
    ex, p50, p95 = (
        cells(rows, "executions_per_s"),
        cells(rows, "step_start_latency_p50_ms"),
        cells(rows, "step_start_latency_p95_ms"),
    )
    rss, cpu, lock = cells(rows, "rss_mb_max"), cells(rows, "cpu_pct_mean"), cells(rows, "lock_wait_share")
    out: list[list[object]] = []
    for w, size in sorted(ex):
        v = ex[(w, size)]
        out.append(
            [
                w,
                size,
                len(v),
                f"{st.median(v):.0f}",
                f"{min(v):.0f}-{max(v):.0f}",
                f"{st.median(p50[(w, size)]):.0f}",
                f"{st.median(p95[(w, size)]):.0f}",
                f"{max(rss[(w, size)]):.0f}",
                f"{st.median(cpu[(w, size)]):.0f}",
                f"{st.median(lock[(w, size)]):.2f}",
            ]
        )
    return out


def baseline_table() -> str:
    root = Path(__file__).parent / "results"
    base, new = defaultdict(list), defaultdict(list)
    for tag, store in (("cmp_base_r", base), ("cmp_new_r", new)):
        for d in sorted(root.glob(f"{tag}*")):
            for r in csv.DictReader((d / "scaling.csv").open()):
                store[(int(r["workers"]), int(r["dag_size"]))].append(f(r, "executions_per_s"))
    if not base or not new:
        return "_comparison data not present_"
    rows = []
    for key in sorted(base):
        b, c = base[key], new[key]
        rows.append(
            [
                key[0],
                key[1],
                len(b),
                f"{st.median(b):.0f} ({min(b):.0f}-{max(b):.0f})",
                f"{st.median(c):.0f} ({min(c):.0f}-{max(c):.0f})",
                f"{st.median(c) / st.median(b):.2f}",
            ]
        )
    return table(
        [
            "workers",
            "DAG nodes",
            "reps",
            "pre-v2 engine exec/s: median (min-max)",
            "v2 engine exec/s: median (min-max)",
            "median ratio v2 / pre-v2",
        ],
        rows,
    )


def main() -> None:
    meta = json.loads((RES / "meta.json").read_text())
    m = meta["machine"]
    scaling, fan, prio, rec = load("scaling"), load("fanout"), load("priority"), load("nested_recovery")
    scaling_chart(scaling)
    fan_data = fanout_chart(fan)
    priority_chart(prio)
    recovery_chart(rec)

    parts = [
        "## Engine v2: scheduling, fan-out, priority, nested recovery\n\n"
        f"Machine: {m['cpu']}, {m['cpu_count']} cores, {m['memory_gb']} GB RAM, {m['platform']}, Python "
        f"{m['python']}, {m['postgres']} in Docker Desktop. Started {meta['started_at']}, took {meta['duration_s']} s. "
        "Real worker processes and an isolated database; every number is derived from rows the engine wrote "
        "(attempt timestamps, `ready_at`, `queue_wait_ms`) plus `psutil` samples of the worker processes and "
        "`pg_stat_activity` samples. **No model calls are made**: nodes are aggregations and calculator tool calls, "
        "so these measure the scheduler and database, not agents. Reproduce: "
        "`cd benchmarks && uv run --group bench python engine_v2_bench.py --label engine_v2`.",
        "### Scaling with worker processes\n\n![scaling](engine_v2_scaling.png)\n\n"
        + table(
            [
                "workers",
                "DAG nodes",
                "reps",
                "exec/s median",
                "min-max",
                "start latency p50 ms (median)",
                "p95 ms (median)",
                "RSS max MB",
                "CPU mean %",
                "lock-wait share",
            ],
            scaling_rows(scaling),
        )
        + "\n\nThe backlog (40 workflows) is pre-filled before workers start, so *start latency* is queueing "
        "delay inside the backlog, not submission delay. *Lock-wait share* is the fraction of sampled active "
        "backends waiting on a PostgreSQL lock (the per-run row lock that serializes event sequencing and state "
        "transitions). *RSS* is summed over all worker processes. Cells are repetitions interleaved over time; "
        "ranges show real run-to-run variance, so treat differences inside them as noise. The compose app "
        "containers (api, web, worker) were stopped during the run.",
        "### Did engine v2 slow the scheduler down? (old vs new engine, same harness)\n\n"
        + baseline_table()
        + "\n\nControl experiment: the benchmark harness and database schema are identical; only the engine code "
        "differs (the pre-v2 commit and the v2 commit are separate frozen `git worktree` checkouts; the old code is "
        "selected with `PYTHONPATH`). Three repetitions per cell, alternating old and new within each repetition so "
        "slow drift in the machine affects both equally.",
        "### Static DAG vs `fan_out` node\n\n![fanout](engine_v2_fanout.png)\n\n"
        + table(
            ["kind", "parallel tasks", "reps", "processing s (mean)", "min", "max"],
            [
                [k, n, len(v), f"{st.fmean(v):.2f}", f"{min(v):.2f}", f"{max(v):.2f}"]
                for (k, n), v in sorted(fan_data.items(), key=lambda kv: (kv[0][1], kv[0][0]))
            ],
        )
        + "\n\nThe same number of calculator tasks, declared statically versus expanded by a `fan_out` node at run "
        "time. The engine-handled expansion adds one extra step row and one `step.fan_out` event per workflow; "
        "differences between the two within the min-max ranges are not significant.",
        "### Priority aging (starvation protection)\n\n![priority](engine_v2_priority.png)\n\n"
        + table(
            [
                "config",
                "rep",
                "high runs",
                "low runs",
                "low latency p50 s",
                "low max s",
                "high latency p50 s",
                "high p95 s",
                "low finished before the stream ended %",
            ],
            [
                [
                    r["config"],
                    r["rep"],
                    r["high_runs"],
                    r["low_runs"],
                    r["low_latency_p50_s"],
                    r["low_latency_max_s"],
                    r["high_latency_p50_s"],
                    r["high_latency_p95_s"],
                    r["low_done_before_stream_ended_pct"],
                ]
                for r in prio
            ],
        )
        + "\n\n6 priority-0 four-step chains are submitted at t=0, then priority-9 chains arrive for 10 s at 8 per "
        "second, faster than one worker serves them. Latency is each run's own submission to completion. **Aging "
        "is accelerated to 0.5 s per priority point** (default 30 s). Without aging the low-priority runs wait "
        "behind every later high-priority arrival; with aging they overtake once their wait exceeds the "
        "priority gap, at the price of slightly higher high-priority latency. **A design lesson:** an earlier "
        "version of this benchmark submitted everything at once and showed no effect, correctly: runs in a "
        "pre-filled backlog age together, so aging cannot close the gap between them. It only protects against "
        "later arrivals, which is exactly the starvation case.",
        "### Crash inside a nested workflow\n\n![nested](engine_v2_nested_recovery.png)\n\n"
        + table(
            [
                "lease TTL s",
                "run",
                "parent",
                "child",
                "victim attempts",
                "detect s",
                "parent done after crash s",
                "duplicate attempts",
            ],
            [
                [
                    r["lease_ttl_s"],
                    r["run"],
                    r["parent_status"],
                    r["child_status"],
                    r["victim_attempts"],
                    r["detect_s"],
                    r["parent_completed_after_crash_s"],
                    r["duplicate_attempts"],
                ]
                for r in rec
            ],
        )
        + "\n\nA worker process is killed (`os._exit(137)`) while running the only step of a child run; the "
        "parent's `sub_workflow` step waits durably. *Detect* is crash → lease-expiry detection; *parent done* "
        "includes the retry in the rescuing worker and the coordinator's child→parent propagation tick.",
    ]
    FRAG.mkdir(parents=True, exist_ok=True)
    (FRAG / "10_engine_v2.md").write_text("\n\n".join(parts) + "\n")
    print("wrote", FRAG / "10_engine_v2.md")


if __name__ == "__main__":
    _ = S3
    main()
