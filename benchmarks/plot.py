"""Render benchmark charts (PNG) from results CSVs: uv run --group bench python benchmarks/plot.py"""

from __future__ import annotations

import csv
import statistics as st
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

RES = Path(__file__).parent / "results"
OUT = Path(__file__).resolve().parents[1] / "docs" / "benchmarks"
# Validated reference palette (light mode) - see dataviz guidance; one hue per entity, fixed order.
S1, S2, S3 = "#2a78d6", "#eb6834", "#1baf7a"
INK, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#e1e4e8", "#fcfcfb"

plt.rcParams.update(
    {
        "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE,
        "axes.edgecolor": GRID,
        "axes.labelcolor": MUTED,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "text.color": INK,
        "axes.grid": True,
        "grid.color": GRID,
        "grid.linewidth": 0.8,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "font.size": 10,
        "legend.frameon": False,
        "axes.axisbelow": True,
    }
)


def load(label: str, name: str) -> list[dict[str, str]]:
    p = RES / label / f"{name}.csv"
    return list(csv.DictReader(p.open())) if p.exists() else []


def save(fig: plt.Figure, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(OUT / f"{name}.png", dpi=150)
    plt.close(fig)
    print("wrote", OUT / f"{name}.png")


def throughput() -> None:
    base, opt = load("baseline", "throughput"), load("optimized", "throughput")
    if not base:
        return
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6), sharey=True)
    for ax, size in zip(axes, ("5", "20"), strict=True):
        for rows, label, color in ((base, "baseline", S1), (opt, "optimized (batched events)", S2)):
            pts = sorted(
                (int(r["workers"]), float(r["steps_per_s"]))
                for r in rows
                if r["dag_size"] == size and r["concurrency"] == "8" and r["node_kind"] == "aggregation"
            )
            if pts:
                ax.plot(
                    [p[0] for p in pts], [p[1] for p in pts], marker="o", lw=2, ms=6, color=color, label=label
                )
                ax.annotate(
                    f"{pts[-1][1]:.0f}",
                    pts[-1],
                    textcoords="offset points",
                    xytext=(6, -3),
                    color=INK,
                    fontsize=9,
                )
        ax.set_title(f"{size}-node fan-out DAG, 40 workflows", fontsize=10, color=INK, loc="left")
        ax.set_xlabel("worker processes (concurrency 8 each)")
        ax.set_xticks([1, 2, 4])
    axes[0].set_ylabel("steps completed per second")
    axes[0].legend(loc="upper left")
    save(fig, "throughput")


def ab() -> None:
    rows = load("ab", "ab")
    if not rows:
        return
    cfgs = sorted({(int(r["workers"]), int(r["dag_size"])) for r in rows}, key=lambda c: (c[1], c[0]))
    fig, ax = plt.subplots(figsize=(7, 3.6))
    width = 0.36
    for i, (mode, label, color) in enumerate(
        (("False", "per-event writes", S1), ("True", "batched events", S2))
    ):
        meds, lo, hi = [], [], []
        for w, n in cfgs:
            xs = [
                float(r["steps_per_s"])
                for r in rows
                if int(r["workers"]) == w and int(r["dag_size"]) == n and r["event_batching"] == mode
            ]
            m = st.median(xs)
            meds.append(m)
            lo.append(m - min(xs))
            hi.append(max(xs) - m)
        xpos = [k + (i - 0.5) * width for k in range(len(cfgs))]
        ax.bar(
            xpos,
            meds,
            width * 0.92,
            color=color,
            label=label,
            yerr=[lo, hi],
            capsize=3,
            error_kw={"ecolor": MUTED, "lw": 1},
        )
        for x, m, h in zip(xpos, meds, hi, strict=True):
            ax.annotate(
                f"{m:.0f}",
                (x, m + h),
                textcoords="offset points",
                xytext=(0, 3),
                ha="center",
                fontsize=8,
                color=INK,
            )
    ax.set_xticks(range(len(cfgs)), [f"{w} workers\n{n}-node DAG" for w, n in cfgs])
    ax.set_ylabel("steps per second (median, min-max)")
    ax.set_title("Event batching A/B, 4 interleaved repetitions", fontsize=10, color=INK, loc="left")
    ax.legend(loc="upper left")
    save(fig, "event_batching_ab")


def parallel() -> None:
    rows = load("optimized", "parallel") or load("baseline", "parallel")
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(6, 3.2))
    modes = ["sequential", "parallel"]
    meds = [st.median(float(r["completion_s"]) for r in rows if r["mode"] == m) for m in modes]
    ideal = [float(next(r["ideal_s"] for r in rows if r["mode"] == m)) for m in modes]
    ax.barh(modes, meds, color=S1, height=0.5, label="measured (median of 5)")
    ax.scatter(ideal, modes, color=S2, marker="|", s=400, lw=2.5, zorder=3, label="ideal (work only)")
    for y, (m, i) in enumerate(zip(meds, ideal, strict=True)):
        ax.annotate(
            f"{m:.2f}s (+{m - i:.2f}s overhead)",
            (m, y),
            textcoords="offset points",
            xytext=(6, -3),
            fontsize=9,
        )
    ax.set_xlabel("completion time, seconds (8 nodes x 0.5 s work)")
    ax.set_xlim(0, max(meds) * 1.45)
    ax.set_title(
        f"Dependency-aware parallelism: {meds[0] / meds[1]:.2f}x speedup", fontsize=10, color=INK, loc="left"
    )
    ax.legend(loc="lower right")
    save(fig, "parallel_vs_sequential")


def recovery() -> None:
    rows = load("optimized", "recovery") or load("baseline", "recovery")
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(6, 3.4))
    ttls = sorted({float(r["lease_ttl_s"]) for r in rows})
    for key, label, color, off in (
        ("detect_s", "failure detected (lease expired)", S1, -0.12),
        ("reclaim_s", "work reclaimed (retry started)", S2, 0.12),
    ):
        for t in ttls:
            xs = [float(r[key]) for r in rows if float(r["lease_ttl_s"]) == t and r[key]]
            ax.scatter(
                [t + off] * len(xs),
                xs,
                color=color,
                s=28,
                alpha=0.8,
                edgecolor=SURFACE,
                lw=1,
                label=label if t == ttls[0] else None,
            )
    ax.plot(ttls, ttls, color=MUTED, lw=1, ls="--", label="lease TTL")
    ax.set_xticks(ttls)
    ax.set_xlabel("lease TTL, seconds (heartbeat = TTL/5)")
    ax.set_ylabel("seconds after the crash")
    ok = sum(r["succeeded"] == "True" for r in rows)
    ax.set_title(
        f"Worker crash recovery: {ok}/{len(rows)} runs recovered", fontsize=10, color=INK, loc="left"
    )
    ax.legend(loc="upper left")
    save(fig, "recovery")


def overhead() -> None:
    rows = load("optimized", "overhead") or load("baseline", "overhead")
    if not rows:
        return
    fig, ax = plt.subplots(figsize=(6, 3.0))
    labels = [f"{r['provider_latency_ms']} ms model" for r in rows]
    model = [float(r["direct_provider_call_ms"]) for r in rows]
    orch = [float(r["orchestration_overhead_p50_ms"]) for r in rows]
    ax.barh(labels, model, color=S1, height=0.5, label="model call (mock latency)")
    ax.barh(
        labels,
        orch,
        left=model,
        color=S2,
        height=0.5,
        label="AgentOS orchestration (p50)",
        edgecolor=SURFACE,
        lw=2,
    )
    for y, (m, o) in enumerate(zip(model, orch, strict=True)):
        ax.annotate(
            f"{o:.0f} ms overhead", (m + o, y), textcoords="offset points", xytext=(6, -3), fontsize=9
        )
    ax.set_xlabel("milliseconds per single-call agent step")
    ax.set_xlim(0, max(m + o for m, o in zip(model, orch, strict=True)) * 1.4)
    ax.set_title("Orchestration overhead per agent step", fontsize=10, color=INK, loc="left")
    ax.legend(loc="lower right")
    save(fig, "overhead")


if __name__ == "__main__":
    throughput()
    ab()
    parallel()
    recovery()
    overhead()
