"""Chart and report fragment for the Chaos Lab benchmark, generated from results/chaos/*.csv."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from plot import INK, S1, S2, plt, save

RES = Path(__file__).parent / "results" / "chaos"
FRAG = Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "fragments"


def load(name: str) -> list[dict[str, str]]:
    return list(csv.DictReader((RES / f"{name}.csv").open()))


def table(headers: list[str], rows: list[list[object]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def pct(v: str | None) -> str:
    return "n/a" if v in (None, "", "None") else f"{float(v) * 100:.0f}%"


def sec(v: str | None) -> str:
    return (
        "n/a"
        if v in (None, "", "None")
        else f"{float(v) / 1000:.2f} s"
        if float(v) >= 1000
        else f"{float(v):.0f} ms"
    )


def chart(rows: list[dict[str, str]]) -> None:
    scen = list(dict.fromkeys(r["scenario"] for r in rows))
    fig, ax = plt.subplots(figsize=(10, 3.8))
    w = 0.38
    for off, policy, color, label in (
        (-w / 2 - 0.01, "no_retry", S1, "no retries (1 attempt per step)"),
        (w / 2 + 0.01, "as_configured", S2, "retries as configured (3 attempts)"),
    ):
        vals = [
            float(
                next(r for r in rows if r["scenario"] == s and r["recovery_policy"] == policy)["completion"]
            )
            for s in scen
        ]
        bars = ax.bar([i + off for i in range(len(scen))], vals, w, color=color, label=label)
        for b, v in zip(bars, vals, strict=True):
            ax.annotate(
                f"{v * 100:.0f}%",
                (b.get_x() + b.get_width() / 2, v),
                textcoords="offset points",
                xytext=(0, 3),
                ha="center",
                fontsize=8,
                color=INK,
            )
    ax.set_xticks(
        range(len(scen)),
        [s.replace(" (no faults)", "\n(no faults)").replace("-", "-\n", 1) for s in scen],
        fontsize=8,
    )
    ax.set_ylim(0, 1.2)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.yaxis.set_major_formatter(lambda v, _: f"{v * 100:.0f}%")
    ax.set_ylabel("runs that completed")
    ax.legend(loc="upper center", ncols=2, fontsize=8)
    ax.set_title(
        "Task completion under injected faults (mock provider; same fault schedule per pair)",
        fontsize=10,
        color=INK,
        loc="left",
    )
    save(fig, "chaos_completion")


def main() -> None:
    rows, eff = load("scenarios"), load("retry_effect")
    meta = json.loads((RES / "meta.json").read_text())
    m = meta["machine"]
    chart(rows)
    body = [
        "## Chaos Lab: resilience scorecards\n\n"
        f"Machine: {m['cpu']}, {m['cpu_count']} cores, {m['memory_gb']} GB, {m['platform']}, Python {m['python']}, {m['postgres']} "
        f"in Docker. Started {meta['started_at']}, took {meta['duration_s']} s. {meta['workers']}, lease TTL "
        f"{meta['lease_ttl_seconds']} s, {meta['trials_per_experiment']} runs per experiment, mock provider, **no model calls**. "
        "Each built-in scenario is run twice with the identical fault schedule: with retries disabled (`no_retry`) and with the "
        "workflow's own retries (`as_configured`). These numbers describe how the *platform* responds to faults, not how a "
        "real model behaves. Reproduce: `cd benchmarks && uv run --group bench python chaos_bench.py --label chaos`.",
        "### Completion with and without retries\n\n![chaos](chaos_completion.png)\n\n"
        + table(
            [
                "scenario",
                "retries",
                "runs",
                "injections",
                "completed",
                "recovery rate",
                "attempts / step",
                "time to detect (median)",
                "time to recover (median / p95, n)",
                "state consistency",
                "errors seen",
            ],
            [
                [
                    r["scenario"],
                    "none" if r["recovery_policy"] == "no_retry" else "as configured",
                    r["runs"],
                    r["injections"],
                    f"{r['completed']}/{r['runs']} ({pct(r['completion'])})",
                    pct(r["recovery_rate"]),
                    f"{float(r['attempts_per_step']):.2f}" if r["attempts_per_step"] else "n/a",
                    sec(r["detect_median_ms"]),
                    f"{sec(r['recover_median_ms'])} / {sec(r['recover_p95_ms'])} (n={r['recover_n']})",
                    pct(r["consistency"]),
                    r["errors"] or "—",
                ]
                for r in rows
            ],
        ),
        "### Is the retry effect real? (paired, per scenario)\n\n"
        + table(
            [
                "scenario",
                "n",
                "no retries",
                "with retries",
                "paired difference",
                "95% CI",
                "McNemar p",
                "supported?",
            ],
            [
                [
                    e["scenario"],
                    e["n"],
                    pct(e["no_retry"]),
                    pct(e["with_retry"]),
                    f"{float(e['diff']):+.3f}" if e["diff"] not in ("", "None") else "n/a",
                    f"[{float(e['ci_low']):+.3f}, {float(e['ci_high']):+.3f}]"
                    if e["ci_low"] not in ("", "None")
                    else "n/a",
                    f"{float(e['mcnemar_p']):.4f}" if e["mcnemar_p"] not in ("", "None") else "n/a",
                    e["significant"],
                ]
                for e in eff
            ],
        )
        + "\n\nA difference is labeled *supported* only when the paired bootstrap interval excludes 0 with at least 10 matched cases "
        "(OpenEval's rule); otherwise it is inconclusive, however large the raw difference looks.",
        "### Safety metrics\n\n"
        + table(
            [
                "scenario",
                "duplicates neutralized",
                "duplicate executions found",
                "policy held under hostile output",
                "state consistency checks",
                "budget compliance",
            ],
            [
                [
                    r["scenario"],
                    f"{r['dup_prevented']}/{r['dup_total']}",
                    r["dup_executions_found"],
                    f"{pct(r['policy_held'])} of {r['policy_runs']} runs"
                    if r["policy_runs"] != "0"
                    else "n/a",
                    f"{pct(r['consistency'])} of {r['consistency_checks']}",
                    pct(r["budget_compliance"]),
                ]
                for r in rows
                if r["recovery_policy"] == "as_configured"
            ],
        )
        + "\n\nThe duplicate and hostile-output rows are evidence that the idempotency fence and tool policy operated, not "
        "that a model is robust to prompt injection.",
    ]
    FRAG.mkdir(parents=True, exist_ok=True)
    (FRAG / "20_chaos.md").write_text("\n\n".join(body) + "\n")
    print("wrote", FRAG / "20_chaos.md")


if __name__ == "__main__":
    main()
