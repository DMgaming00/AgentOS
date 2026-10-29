"""Draw the real-model headline chart from the saved result files (no numbers are typed in).

    uv run --group bench python benchmarks/real_results_chart.py   ->  docs/benchmarks/real_results.png
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

R = Path(__file__).resolve().parent / "results" / "real_haiku"
OUT = Path(__file__).resolve().parent.parent / "docs" / "benchmarks" / "real_results.png"
BASE, CAND, ALT, INK, MUTED = "#8a94a6", "#2563eb", "#d97706", "#111827", "#6b7280"


def load(name: str):
    return json.loads((R / name).read_text())


def bars(ax, labels, values, colors, fmt, ylim=(0, 1.22), notes=None):
    xs = range(len(labels))
    b = ax.bar(xs, values, color=colors, width=0.62)
    for i, (rect, v) in enumerate(zip(b, values, strict=True)):
        ax.text(rect.get_x() + rect.get_width() / 2, v + 0.02, fmt(v), ha="center", va="bottom", fontsize=10, color=INK, fontweight="bold")
        if notes:
            low = v < 0.12
            ax.text(rect.get_x() + rect.get_width() / 2, (v + 0.09) if low else 0.02, notes[i], ha="center", va="bottom", fontsize=8, color=MUTED if low else "white")
    ax.set_xticks(list(xs), labels, fontsize=9)
    ax.set_ylim(*ylim)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0], ["0%", "25%", "50%", "75%", "100%"], fontsize=8, color=MUTED)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.grid(axis="y", color="#e5e7eb", linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def main() -> None:
    hard = load("hard_results.json")
    val = next(x for x in hard if x["kind"] == "paired" and x["split"] == "validation")
    test = next(x for x in hard if x["kind"] == "paired" and x["split"] == "test")
    careful = next(x for x in hard if x["kind"] == "eval" and x["agent"] == "careful")
    sonnet = next(x for x in hard if x["kind"] == "eval" and x["agent"] == "sonnet")
    chaos = {e["label"]: e for e in load("chaos_n20.json")["experiments"]}
    chaos6 = {e["label"]: e for e in load("chaos.json")["experiments"]}
    mem: dict[str, list[int]] = {}
    for f in ("memory.json", "memory_seeds4-10_churn12-5.json"):
        for run in load(f)["runs"]:
            for k, v in run["results"]["strategies"].items():
                a = v["accuracy"]
                m = mem.setdefault(k, [0, 0])
                m[0] += a["numerator"]
                m[1] += a["denominator"]

    fig, axs = plt.subplots(2, 2, figsize=(13, 8.6), facecolor="white")
    fig.suptitle("AgentOS on real Claude models: headline results", fontsize=15, fontweight="bold", color=INK, x=0.06, ha="left", y=0.985)
    fig.text(0.06, 0.945, "Small, single-run demonstrations (see the README for sample sizes and caveats). Estimated total cost about $14.", fontsize=9.5, color=MUTED)

    ax = axs[0, 0]
    ax.set_title("Autopilot repairs a flawed agent (3-step budget)", fontsize=11, loc="left", color=INK)
    vals = [val["baseline"]["value"], val["candidate_metrics"]["value"], test["baseline"]["value"], test["candidate_metrics"]["value"]]
    bars(ax, ["validation\nbefore", "validation\nafter", "held-out test\nbefore", "held-out test\nafter"], vals, [BASE, CAND, BASE, CAND], lambda v: f"{v:.0%}")
    ax.text(0.5, 0.99, f"+{val['comparison']['diff_mean']:.2f}  CI [{val['comparison']['ci95'][0]:+.2f}, {val['comparison']['ci95'][1]:+.2f}]", transform=ax.transAxes, ha="center", fontsize=8.5, color=MUTED, va="top")

    ax = axs[0, 1]
    ax.set_title("Chaos Lab: do retries rescue runs? (completed runs)", fontsize=11, loc="left", color=INK)
    items = [
        ("brownout\nretries off", chaos["Haiku brownout, retries off"], BASE),
        ("brownout\nretries on", chaos["Haiku brownout, retries on"], CAND),
        ("crashes, 3 att.\n(n=6)", chaos6["Haiku worker crashes, retries on"], BASE),
        ("crashes, 5 att.\n(n=6)", chaos6["Haiku worker crashes, 5 attempts"], CAND),
    ]
    vs = [e["completion"]["value"] for _, e, _ in items]
    notes = [f"{e['completion']['numerator']}/{e['completion']['denominator']}" for _, e, _ in items]
    bars(ax, [n for n, _, _ in items], vs, [c for _, _, c in items], lambda v: f"{v:.0%}", notes=notes)
    ax.text(0.5, 0.99, "n=20 for brownout; crash loop at n=20 hit the 10-min cap", transform=ax.transAxes, ha="center", fontsize=8.5, color=MUTED, va="top")

    ax = axs[1, 0]
    ax.set_title("Memory Lab: real Haiku reader (380 questions per strategy)", fontsize=11, loc="left", color=INK)
    order = [("none", "no\nmemory"), ("short_term", "short-\nterm"), ("summarized_stale", "stale\nsummaries"), ("summarized_refreshed", "refreshed\nsummaries"), ("persistent_full", "all\nrecords"), ("retrieval_top3", "retrieval\ntop-3")]
    vs = [mem[k][0] / mem[k][1] for k, _ in order]
    cols = [BASE, BASE, ALT, CAND, CAND, CAND]
    bars(ax, [n for _, n in order], vs, cols, lambda v: f"{v:.0%}")

    ax = axs[1, 1]
    ax.set_title("Haiku vs Sonnet, same prompt (50 validation cases, 1 trial)", fontsize=11, loc="left", color=INK)
    h, s = careful["metrics"], sonnet["metrics"]
    vs = [h["task_success_rate"]["value"], s["task_success_rate"]["value"]]
    bars(ax, [f"Haiku 5.5\n${h['cost_per_successful_task_usd']:.3f} per success", f"Sonnet 5.5\n${s['cost_per_successful_task_usd']:.3f} per success"], vs, [CAND, ALT], lambda v: f"{v:.0%}")
    ax.text(0.5, 0.99, "about 10x the cost per success for about +10 points", transform=ax.transAxes, ha="center", fontsize=8.5, color=MUTED, va="top")

    fig.tight_layout(rect=(0.02, 0.0, 0.99, 0.93))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT, dpi=140)
    print("wrote", OUT)


if __name__ == "__main__":
    main()
