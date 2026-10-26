"""Charts + Markdown report for the Autopilot benchmark, generated from results/autopilot/ (no hand-copied numbers).

uv run --group bench python benchmarks/autopilot_report.py   # writes docs/autopilot-benchmarks.md + docs/benchmarks/autopilot_*.png
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from plot import GRID, INK, MUTED, S1, S2, plt, save

RES = Path(__file__).parent / "results" / "autopilot"
DOC = Path(__file__).resolve().parents[1] / "docs" / "autopilot-benchmarks.md"


def load(name: str) -> list[dict[str, str]]:
    return list(csv.DictReader((RES / f"{name}.csv").open()))


def f(v: str) -> float | None:
    return float(v) if v not in ("", "None") else None


def pct(v: str | float | None) -> str:
    return "n/a" if v in (None, "", "None") else f"{float(v) * 100:.1f}%"


def money(v: str) -> str:
    return "undefined (0 successes)" if v in ("", "None") else f"${float(v):.5f}"


def table(headers: list[str], data: list[list[object]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in data])


def label_bars(ax: plt.Axes, bars: object, values: list[float]) -> None:
    for b, v in zip(bars, values, strict=True):  # type: ignore[call-overload]
        ax.annotate(
            f"{v * 100:.0f}%",
            (b.get_x() + b.get_width() / 2, v),
            textcoords="offset points",
            xytext=(0, 3),
            ha="center",
            fontsize=9,
            color=INK,
        )


def success_chart(arms: list[dict[str, str]]) -> None:
    groups = [
        ("Fix: JSON numbers\n(validation)", arms[0], arms[1]),
        ("Fast path\n(validation)", arms[2], arms[3]),
        ("Promoted v2\n(held-out test)", arms[4], arms[5]),
    ]
    fig, ax = plt.subplots(figsize=(7, 3.4))
    x = range(len(groups))
    w = 0.36
    base = [float(g[1]["task_success"]) for g in groups]
    cand = [float(g[2]["task_success"]) for g in groups]
    b1 = ax.bar([i - w / 2 - 0.01 for i in x], base, w, color=S1, label="baseline v1")
    b2 = ax.bar([i + w / 2 + 0.01 for i in x], cand, w, color=S2, label="candidate")
    label_bars(ax, b1, base)
    label_bars(ax, b2, cand)
    ax.set_xticks(list(x), [g[0] for g in groups])
    ax.set_ylim(0, 1.3)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.yaxis.set_major_formatter(lambda v, _: f"{v * 100:.0f}%")
    ax.set_ylabel("task success (deterministic graders)")
    ax.set_title(
        f"Task success, {arms[0]['trials']} trials per case (mock provider)",
        fontsize=10,
        color=INK,
        loc="left",
    )
    ax.legend(loc="upper right", ncols=2)
    save(fig, "autopilot_success")


def ci_chart(arms: list[dict[str, str]]) -> None:
    rows = [r for r in arms if r.get("diff_mean")]
    fig, ax = plt.subplots(figsize=(9, 2.4))
    for i, r in enumerate(rows):
        lo, hi, m = float(r["ci_low"]), float(r["ci_high"]), float(r["diff_mean"])
        ax.plot([lo, hi], [i, i], color=MUTED, lw=2, solid_capstyle="round")
        ax.plot([m], [i], "o", color=S2, ms=8, mec="white", mew=2)
        ax.annotate(
            f"{m:+.3f}  [{lo:+.3f}, {hi:+.3f}]  → {r['decision']}",
            (hi, i),
            textcoords="offset points",
            xytext=(8, -3),
            fontsize=9,
            color=INK,
        )
    ax.axvline(0, color=INK, lw=1)
    ax.set_yticks(range(len(rows)), [r["candidate"] for r in rows])
    ax.set_xlim(-1, 2.4)
    ax.set_xticks([-1, -0.5, 0, 0.5, 1])
    ax.set_ylim(-0.6, len(rows) - 0.4)
    ax.grid(axis="y", visible=False)
    ax.set_xlabel("candidate minus baseline case success (paired, 95% bootstrap CI)")
    ax.set_title("Paired comparison on the validation split", fontsize=10, color=INK, loc="left")
    save(fig, "autopilot_ci")


def live_chart(live: dict[str, object]) -> None:
    keys = [
        ("before_regression_failure_rate", "v2 live"),
        ("regressed_failure_rate", "v3 live\n(max_steps=2)"),
        ("after_rollback_failure_rate", "after rollback\nto v2"),
    ]
    vals = [float(live[k]) for k, _ in keys]  # type: ignore[arg-type]
    fig, ax = plt.subplots(figsize=(5.5, 3.0))
    bars = ax.bar([lbl for _, lbl in keys], vals, 0.5, color=S1)
    label_bars(ax, bars, vals)
    ax.set_ylim(0, 1.15)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v * 100:.0f}%")
    ax.set_ylabel(f"failed live runs (n={live['runs_per_phase']} each)")
    ax.set_title("Live regression and rollback", fontsize=10, color=INK, loc="left")
    save(fig, "autopilot_rollback")


def concurrency_chart(scan: list[dict[str, str]]) -> None:
    fig, ax = plt.subplots(figsize=(5.5, 3.0))
    xs = [int(r["eval_max_concurrency"]) for r in scan]
    ys = [float(r["wall_s"]) for r in scan]
    ax.plot(xs, ys, color=S1, lw=2, marker="o", ms=8, mec="white", mew=2)
    for x, y in zip(xs, ys, strict=True):
        ax.annotate(f"{y:.1f}s", (x, y), textcoords="offset points", xytext=(6, 4), fontsize=9)
    ax.set_xscale("log", base=2)
    ax.set_xticks(xs, [str(x) for x in xs])
    ax.set_ylim(0, max(ys) * 1.2)
    ax.set_xlabel("evaluation max_concurrency (2 workers)")
    ax.set_ylabel("wall time, seconds")
    ax.set_title(
        f"Validation split ({scan[0]['executions']} executions) wall time", fontsize=10, color=INK, loc="left"
    )
    ax.spines["bottom"].set_color(GRID)
    save(fig, "autopilot_eval_concurrency")


def main() -> None:
    arms, phases, scan = load("arms"), load("phases"), load("eval_concurrency")
    res = json.loads((RES / "results.json").read_text())
    meta = json.loads((RES / "meta.json").read_text())
    success_chart(arms)
    ci_chart(arms)
    live_chart(res["live"])
    concurrency_chart(scan)
    m = meta["machine"]
    cands = res["candidates"]
    rejected_policy = [c for c in cands if c["status"] == "rejected_policy"]
    parts = [
        "# Autopilot benchmark\n\n_Generated by `benchmarks/autopilot_report.py` from `benchmarks/results/autopilot/`. "
        "Do not edit by hand._",
        "## What this measures and what it does not\n\nOne full optimization cycle (observe → detect → diagnose → propose → "
        "experiment → gate → approve → deploy → monitor → rollback) on an isolated database with real worker processes, then an "
        "evaluation-concurrency scan. **The provider is the deterministic mock** (`mock-extractor`, scripted diagnosis and "
        "optimizer), so success rates describe the scripted demo agent's config-sensitive behavior, not a real model, and costs "
        "are simulated from the mock price table. What the numbers do show is that the pipeline measures, gates and rolls back "
        "correctly, and how long its own machinery takes.",
        f"Machine: {m['cpu']}, {m['cpu_count']} cores, {m['memory_gb']} GB RAM, {m['platform']}, Python {m['python']}, "
        f"{m['postgres']}. Run started {meta['started_at']}, took {meta['duration_s']}s. Trials per case: {meta['trials']}. "
        "Bootstrap: paired percentile, 10,000 resamples, seed 20261025.",
        "## Reproduce\n\n```bash\nmake up            # postgres + redis\nuv run --group bench python benchmarks/autopilot_bench.py "
        "--label autopilot --trials 3\nuv run --group bench python benchmarks/autopilot_report.py\n```",
        "## Baseline vs candidate\n\n![success](benchmarks/autopilot_success.png)\n\n"
        + table(
            [
                "arm",
                "split",
                "candidate",
                "executions",
                "task success",
                "schema",
                "grounding",
                "tool correctness",
                "cost / success",
                "total cost",
                "p50 ms",
                "p95 ms",
            ],
            [
                [
                    r["arm"],
                    r["split"],
                    r["candidate"] or "—",
                    r["executions"],
                    f"{pct(r['task_success'])} ({r['success_numerator']}/{r['success_denominator']})",
                    pct(r["schema_compliance"]),
                    pct(r["grounding"]),
                    pct(r["tool_correctness"]),
                    money(r["cost_per_success_usd"]),
                    f"${float(r['total_cost_usd']):.5f}",
                    f"{float(r['latency_p50_ms']):.0f}",
                    f"{float(r['latency_p95_ms']):.0f}",
                ]
                for r in arms
            ],
        ),
        "Baseline rows are separate executions per experiment (each experiment runs both arms interleaved), so baseline latency "
        "differs slightly between rows. The held-out test split was evaluated only after selection, and was never shown to the "
        "optimizer.",
        "## Paired comparison and gate decisions\n\n![ci](benchmarks/autopilot_ci.png)\n\n"
        + table(
            [
                "candidate",
                "decision",
                "matched cases",
                "regressed cases",
                "per-case regression rate",
                "mean diff",
                "95% CI",
            ],
            [
                [
                    r["candidate"],
                    r["decision"],
                    int(r["executions"]) // int(r["trials"]),
                    r["regressions"],
                    pct(r["regression_rate"]),
                    f"{float(r['diff_mean']):+.4f}",
                    f"[{float(r['ci_low']):+.4f}, {float(r['ci_high']):+.4f}]",
                ]
                for r in arms
                if r.get("diff_mean")
            ],
        ),
        f"Candidates generated: {len(cands)}. Rejected by policy before any evaluation: {len(rejected_policy)} "
        + "("
        + "; ".join(f"“{c['title']}”: {', '.join(c['violations'])}" for c in rejected_policy)
        + "). "
        "Evaluated: "
        + ", ".join(f"“{k}” → {v}" for k, v in res["decisions"].items())
        + f". The fast-path candidate is {float(arms[2]['total_cost_usd']) / float(arms[3]['total_cost_usd']):.1f}x "
        "cheaper in total but fails every case, so its cost per successful task is undefined, not low, and it is refused.",
        "## Live regression and rollback\n\n![rollback](benchmarks/autopilot_rollback.png)\n\n"
        + table(
            ["phase", "failed live runs"],
            [
                ["v2 (promoted fix)", pct(res["live"]["before_regression_failure_rate"])],
                [
                    "v3 (max_steps=2, passed offline gates: "
                    + res["live"]["regressed_candidate_offline_decision"]
                    + ")",
                    pct(res["live"]["regressed_failure_rate"]),
                ],
                ["after admin rollback to v2", pct(res["live"]["after_rollback_failure_rate"])],
            ],
        )
        + f"\n\n{res['live']['runs_per_phase']} runs per phase of the two-subject live workflow. The step cap passed the "
        "single-subject validation set but breaks the two-subject live workflow, which is the case offline evaluation can "
        "miss and monitoring exists for. Rollback is manual (auto-rollback is off by default) and applies to new runs only.",
        "## Optimization overhead\n\n"
        + table(["phase", "seconds"], [[p["phase"].removesuffix("_s"), p["seconds"]] for p in phases])
        + f"\n\nSimulated Autopilot LLM spend for the cycle (diagnosis + optimizer): ${res['autopilot_llm_spend_usd_simulated']:.6f}. "
        "Diagnosis and candidate generation take milliseconds with the mock provider; with a real provider they are dominated "
        "by model latency, which this benchmark does not measure. Experiment time is dominated by running "
        "2 arms x 16 cases x 3 trials = 96 real workflow runs.",
        "## Evaluation throughput vs concurrency\n\n![concurrency](benchmarks/autopilot_eval_concurrency.png)\n\n"
        + table(
            ["max_concurrency", "executions", "wall s", "executions / s"],
            [[r["eval_max_concurrency"], r["executions"], r["wall_s"], r["executions_per_s"]] for r in scan],
        ),
    ]
    DOC.write_text("\n\n".join(parts) + "\n")
    print("wrote", DOC)


if __name__ == "__main__":
    main()
