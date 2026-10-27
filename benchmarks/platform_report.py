"""Charts and a report fragment for the platform benchmarks, generated from results/platform/*.csv."""

from __future__ import annotations

import csv
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

from plot import INK, S1, S2, S3, plt, save

RES = Path(__file__).parent / "results" / "platform"
FRAG = Path(__file__).resolve().parents[1] / "docs" / "benchmarks" / "fragments"
STRATEGIES = [
    "none",
    "short_term",
    "persistent_full",
    "summarized_stale",
    "summarized_refreshed",
    "retrieval_top3",
]
LABEL = {
    "none": "no memory",
    "short_term": "short-term",
    "persistent_full": "persistent (all)",
    "summarized_stale": "summaries (stale)",
    "summarized_refreshed": "summaries (refreshed)",
    "retrieval_top3": "retrieval top-3",
}


def load(name: str) -> list[dict[str, str]]:
    return list(csv.DictReader((RES / f"{name}.csv").open()))


def table(headers: list[str], rows: list[list[object]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    return "\n".join(out + ["| " + " | ".join(str(c) for c in r) + " |" for r in rows])


def ms(vals: list[float], digits: int = 2) -> str:
    if len(vals) == 1:
        return f"{vals[0]:.{digits}f}"
    return f"{st.fmean(vals):.{digits}f} ± {st.stdev(vals):.{digits}f}"


def rng(vals: list[float], digits: int = 2) -> str:
    return f"{st.median(vals):.{digits}f} ({min(vals):.{digits}f}-{max(vals):.{digits}f})"


def pct(v: float) -> str:
    return f"{v * 100:.0f}%"


# ----------------------------------------------------------------------------- memory


def memory() -> str:
    rows = load("memory")
    by: dict[tuple[int, int, str], list[dict[str, str]]] = defaultdict(list)
    for r in rows:
        by[(int(r["updated_facts"]), int(r["expired_facts"]), r["strategy"])].append(r)
    churn = sorted({(u, e) for u, e, _ in by})
    seeds = sorted({r["seed"] for r in rows})
    # accuracy vs churn
    fig, ax = plt.subplots(figsize=(9, 4))
    styles = [
        ("o", "-", S1),
        ("s", "--", S2),
        ("^", "-", S3),
        ("D", "--", INK),
        ("v", "-", "#7a5af8"),
        ("P", "--", "#b0701a"),
    ]
    for strat, (mk, ls, col) in zip(STRATEGIES, styles, strict=True):
        xs = [u + e for u, e in churn]
        ys = [st.fmean(float(r["accuracy"]) for r in by[(u, e, strat)]) for u, e in churn]
        ax.plot(xs, ys, marker=mk, ls=ls, color=col, lw=2, ms=6, label=LABEL[strat])
    ax.set_xlabel("facts changed or expired between the two sessions (of 26 stored)")
    ax.set_ylabel("accuracy (38 questions)")
    ax.set_ylim(0, 1.05)
    ax.yaxis.set_major_formatter(lambda v, _: f"{v * 100:.0f}%")
    ax.legend(ncols=3, fontsize=8, loc="lower left")
    ax.set_title(
        "Memory strategies: accuracy as stored facts go stale (scripted reader)",
        fontsize=10,
        color=INK,
        loc="left",
    )
    save(fig, "platform_memory_accuracy")
    out = [
        "## Memory Lab: strategies under fact churn\n\n![memory](platform_memory_accuracy.png)\n\n"
        f"Each cell is {len(seeds)} seeded scenarios (seeds {', '.join(seeds)}); every (strategy, question) is a graded workflow run on real workers. "
        "The reader is a scripted policy with fixed matching rules, so this measures how each strategy's *context* affects it, not how a real model uses memory. "
        "In the chart the persistent, refreshed-summary and retrieval lines overlap exactly (same accuracy at every churn level); no-memory and short-term rise with churn only because more questions have the correct answer *unknown*. Churn levels (facts updated, facts expired): "
        + ", ".join(f"({u},{e})" for u, e in churn)
        + ". Reproduce: `cd benchmarks && uv run --group bench python platform_bench.py memory --label platform`."
    ]
    for u, e in churn:
        out.append(
            f"### Churn: {u} updated, {e} expired\n\n"
            + table(
                [
                    "strategy",
                    "accuracy",
                    "stale answers (of N)",
                    "wrong-entity reuse (of 4)",
                    "retrieval precision",
                    "answer recall",
                    "context chars (mean)",
                ],
                [
                    [
                        LABEL[s],
                        ms([float(r["accuracy"]) for r in by[(u, e, s)]], 3),
                        f"{st.fmean(int(r['stale_errors']) for r in by[(u, e, s)]):.0f} / {by[(u, e, s)][0]['stale_denominator']}",
                        f"{st.fmean(int(r['wrong_entity_reuse']) for r in by[(u, e, s)]):.0f}",
                        "n/a"
                        if by[(u, e, s)][0]["precision"] in ("", "None")
                        else ms([float(r["precision"]) for r in by[(u, e, s)]], 3),
                        ms([float(r["answer_recall"]) for r in by[(u, e, s)]], 3),
                        f"{st.fmean(float(r['context_chars_mean']) for r in by[(u, e, s)]):.0f}",
                    ]
                    for s in STRATEGIES
                ],
            )
        )
    out.append(
        "Findings the data supports: stale-summary errors scale with churn while current-state strategies have none; every strategy that stores the sibling entity reuses it for the look-alike questions (memory does not always help); "
        "retrieval matches full-context accuracy with a fraction of the context. Variation across seeds is small because the scenario *structure* is fixed by the churn level; what varies by seed is which facts change."
    )
    return "\n\n".join(out)


# ----------------------------------------------------------------------------- security


def security() -> str:
    rows = load("security")
    n = len(rows)

    def col(k: str) -> list[float]:
        return [float(r[k]) for r in rows]

    spec = [
        ("policy tests passed", "policy_pass", "policy_total"),
        ("injection attacks contained", "injection_pass", "injection_total"),
        ("benign controls not blocked", "benign_pass", "benign_total"),
        ("trace secret-exposure checks passed", "exposure_pass", "exposure_total"),
    ]
    lines = [
        [name, f"{int(st.median(col(a)))}/{int(rows[0][b])}", f"{int(min(col(a)))}-{int(max(col(a)))}"]
        for name, a, b in spec
    ]
    return (
        "## Security Center: enforcement under a labeled attack corpus\n\n"
        f"{n} full evaluations on an isolated database (real workflows on real workers, a deliberately gullible mock model that obeys every injected directive). "
        "Reproduce: `cd benchmarks && uv run --group bench python platform_bench.py security --label platform`.\n\n"
        + table(["measure", "median over runs", "min-max over runs"], lines)
        + "\n\n"
        + table(
            ["metric", "value (every run)"],
            [
                [
                    "attacks actually attempted",
                    f"{int(rows[0]['attacks_exercised'])}/{int(rows[0]['attacks_total'])} in {n}/{n} runs"
                    if len({r["attacks_exercised"] for r in rows}) == 1
                    else "varies",
                ],
                [
                    "unauthorized actions attempted / blocked",
                    f"{int(rows[0]['unauthorized_action_attempts'])} / {int(rows[0]['blocked_actions'])}",
                ],
                [
                    "attacks with no harmful effect",
                    f"{int(rows[0]['successful_enforcement'])}/{int(rows[0]['enforcement_denominator'])}",
                ],
                [
                    "unsafe side effects",
                    f"{max(int(r['unsafe_side_effects']) for r in rows)} (max over runs)",
                ],
                [
                    "false-positive blocks (benign controls)",
                    f"{max(int(r['false_positive_blocks']) for r in rows)}/{rows[0]['false_positive_denominator']} (max over runs)",
                ],
                [
                    "cross-workspace canary leaks",
                    f"{max(int(r['canary_leaks']) for r in rows)} (max over runs)",
                ],
                [
                    "planted secrets found in persisted traces",
                    f"{max(int(r['secrets_in_traces']) for r in rows)}/{rows[0]['secret_kinds_planted']} (max over runs)",
                ],
                ["evaluation wall time", f"{rng(col('wall_s'), 1)} s"],
            ],
        )
        + "\n\nThis measures the platform's code-level enforcement against a fixed corpus of 28 attacks (7 goals x 4 surfaces) and 8 benign controls. "
        "It is **not** evidence that a real model resists prompt injection, and a corpus this small is a regression floor, not a guarantee."
    )


# ----------------------------------------------------------------------------- deployment


def deployment() -> str:
    acc = load("routing_accuracy")
    fig, ax = plt.subplots(figsize=(8, 3.6))
    targets = sorted({int(r["target_percent"]) for r in acc})
    for seed, (mk, col) in zip(
        sorted({r["seed"] for r in acc}), (("o", S1), ("s", S2), ("^", S3), ("D", INK)), strict=False
    ):
        pts = sorted(
            (int(r["target_percent"]), float(r["achieved_share"]) * 100 - int(r["target_percent"]))
            for r in acc
            if r["seed"] == seed
        )
        ax.plot(
            [p[0] for p in pts],
            [p[1] for p in pts],
            marker=mk,
            ls="none",
            color=col,
            label=f"seed {seed}",
            ms=7,
        )
    half = {
        int(r["target_percent"]): float(r["binomial_95_halfwidth_points"])
        for r in acc
        if r["seed"] == acc[0]["seed"]
    }
    ax.fill_between(
        targets,
        [-half[t] for t in targets],
        [half[t] for t in targets],
        color=S1,
        alpha=0.12,
        label="95% binomial band",
    )
    ax.axhline(0, color=INK, lw=0.8)
    ax.set_xlabel("target canary percent")
    ax.set_ylabel("achieved - target (percentage points)")
    ax.legend(fontsize=8, ncols=3)
    ax.set_title(
        f"Canary routing accuracy over {acc[0]['n']} run ids per seed", fontsize=10, color=INK, loc="left"
    )
    save(fig, "platform_routing")
    rc = load("rollback_characteristics")
    fig, ax = plt.subplots(figsize=(8, 3.8))
    for n, (mk, col) in zip((10, 20, 40, 80), (("o", S1), ("s", S2), ("^", S3), ("D", INK)), strict=True):
        pts = sorted(
            (float(r["canary_failure_rate"]), float(r["either_rule_trigger"]))
            for r in rc
            if int(r["samples_per_arm"]) == n
        )
        ax.plot(
            [p[0] for p in pts], [p[1] for p in pts], marker=mk, color=col, lw=2, label=f"{n} steps per arm"
        )
    ax.axvline(0.05, color=INK, lw=0.8, ls=":")
    ax.set_xlabel("canary failure rate (baseline 5%, dotted)")
    ax.set_ylabel("share of trials that roll back")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v * 100:.0f}%")
    ax.legend(fontsize=8)
    ax.set_title(
        "Rollback rules: operating characteristics (real rule code, seeded synthetic arms)",
        fontsize=10,
        color=INK,
        loc="left",
    )
    save(fig, "platform_rollback")
    live = load("live_release_trials")
    d = [float(r["detect_after_traffic_s"]) for r in live]
    return (
        "## Release Center: routing, rollback rules and version consistency\n\n"
        "![routing](platform_routing.png)\n\n"
        f"**Canary routing correctness.** `canary_bucket` over {acc[0]['n']} random run ids per seed, {len(targets)} target percentages, 4 seeds: "
        f"the achieved share is within the 95% binomial band in {sum(r['within_95_ci'] == 'True' for r in acc)}/{len(acc)} cells (about 95% expected by construction), "
        f"the largest absolute error is {max(float(r['abs_error_points']) for r in acc):.3f} percentage points, routing is deterministic for a given (run, agent, seed), and the sets are nested "
        f"(a run routed at p% is routed at any larger p) in {sum(r['nested_with_larger_percent'] == 'True' for r in acc)}/{len(acc)} cells.\n\n"
        "![rollback](platform_rollback.png)\n\n"
        "**Rollback rules: operating characteristics.** The real `failure_rate_increase` and `error_budget_exhausted` rule code (default thresholds) run on seeded synthetic arms "
        f"({rc[0]['trials']} trials per cell); this characterizes the configured rules, not any real workload. 'either' = a rollback is triggered by either rule.\n\n"
        + table(
            [
                "steps per arm",
                "canary failure rate",
                "rollback (either rule)",
                "failure-rate rule",
                "error-budget rule (95% SLO)",
                "insufficient evidence",
                "case",
            ],
            [
                [
                    r["samples_per_arm"],
                    pct(float(r["canary_failure_rate"])),
                    pct(float(r["either_rule_trigger"])),
                    pct(float(r["failure_rate_rule_trigger"])),
                    pct(float(r["error_budget_rule_trigger"])),
                    pct(float(r["insufficient_evidence_share"])),
                    "healthy (false rollback)" if r["is_healthy_case"] == "True" else "regression",
                ]
                for r in rc
            ],
        )
        + "\n\nThe baseline fails 5%, which is also the SLO boundary of the error-budget rule, so a canary at 5% is *healthy*. "
        '**A first version of this benchmark exposed a flaw**: with a plain "failures exceed the budget" test, a healthy canary was rolled back 28-37% of the time at 20+ steps per arm. '
        "Both rules now also require statistical evidence (one-sided Fisher exact test for the difference between arms, one-sided binomial test against the SLO boundary, alpha 0.05); "
        "the table above is from the fixed code, where false rollbacks stay at about the alpha level or below and small samples report `insufficient evidence` instead of acting on noise."
        + f"\n\n**Live releases** ({len(live)} repetitions: gates, admin approval, 50% seeded canary, 30 live runs through 3 real workers, automatic rollback by the coordinator, then 10 post-rollback runs):\n\n"
        + table(
            ["measure", "result"],
            [
                [
                    "automatic rollback triggered",
                    f"{sum(r['automatic'] == 'True' for r in live)}/{len(live)}",
                ],
                [
                    "canary-arm failures (at the step limit)",
                    f"{sum(int(r['canary_failures']) for r in live)}/{sum(int(r['canary_runs']) for r in live)} canary runs",
                ],
                ["rollback decided after last live run finished (median / min-max)", f"{rng(d, 2)} s"],
                [
                    "runs pinned to a version different from the seeded routing decision",
                    f"{sum(int(r['pin_mismatches']) for r in live)}/{sum(int(r['canary_runs']) + int(r['baseline_runs']) for r in live)}",
                ],
                [
                    "active/canary pointers consistent after rollback",
                    f"{sum(r['restored_version_consistent'] == 'True' for r in live)}/{len(live)}",
                ],
                [
                    "post-rollback runs succeeded on the restored version",
                    ", ".join(r["post_rollback_runs_ok_on_restored"] for r in live),
                ],
            ],
        )
        + "\n\nThe detection time above is dominated by the coordinator tick interval, not by the rule computation. The canary workload fails deterministically (the step limit), so this shows the mechanism end to end, not detection of subtle regressions (see the operating characteristics for those)."
    )


# ----------------------------------------------------------------------------- optimization


def optimization() -> str:
    rows = load("optimization")
    n = len(rows)

    def col(k: str) -> list[float]:
        return [float(r[k]) for r in rows]

    phases = [
        ("observe (dev benchmark, 9 cases)", "observe_dev_s"),
        ("detect failures", "detect_s"),
        ("diagnose", "diagnose_s"),
        ("generate: rule engine", "generate_rules_s"),
        ("generate: model-style optimizer (mock)", "generate_model_s"),
        ("experiment: rule fix (16 cases x 2 arms)", "experiment_rule_fix_s"),
        ("experiment: cheaper-but-worse candidate", "experiment_fast_path_s"),
    ]
    fig, ax = plt.subplots(figsize=(8, 3.4))
    meds = [st.median(col(k)) for _, k in phases]
    ax.barh([p[0] for p in phases][::-1], meds[::-1], color=S1, height=0.55)
    for y, v in enumerate(meds[::-1]):
        ax.annotate(f"{v:.2f} s", (v, y), textcoords="offset points", xytext=(4, -3), fontsize=8, color=INK)
    ax.set_xlabel(f"seconds (median of {n} repetitions)")
    ax.set_title("Where an optimization cycle spends its time", fontsize=10, color=INK, loc="left")
    save(fig, "platform_optimization")
    r0 = rows[0]
    return (
        "## Optimization Lab: baseline vs candidate on held-out cases\n\n![optimization](platform_optimization.png)\n\n"
        f"{n} full cycles on fresh isolated databases (flawed baseline → failure detection → diagnosis → rule-engine candidate and the model-style optimizer's candidates → paired experiments on the 16-case held-out validation split). "
        "Mock provider: quality numbers describe the scripted demo agent. Reproduce: `cd benchmarks && uv run --group bench python platform_bench.py optimization --label platform`.\n\n"
        + table(
            ["measure", "result (identical across repetitions unless a range is shown)"],
            [
                [
                    "baseline success (development / validation)",
                    f"{rng(col('baseline_dev_success'), 3)} / {rng(col('rule_fix_baseline_success'), 3)}",
                ],
                [
                    "rule-engine candidate: success on validation",
                    f"{rng(col('rule_fix_candidate_success'), 3)}",
                ],
                [
                    "rule-engine candidate: decision",
                    ", ".join(sorted({r["rule_fix_decision"] for r in rows})),
                ],
                [
                    "rule-engine candidate: 95% CI lower bound of the paired difference",
                    rng(col("rule_fix_ci_low"), 3),
                ],
                [
                    "rule-engine candidate: per-case regressions",
                    ", ".join(sorted({r["rule_fix_regressions"] for r in rows})),
                ],
                [
                    "cheaper-but-worse candidate: decision / per-case regressions detected",
                    f"{', '.join(sorted({r['fast_path_decision'] for r in rows}))} / {rng(col('fast_path_regressions'), 0)}",
                ],
                [
                    "cheaper-but-worse candidate: success vs baseline",
                    f"{rng(col('fast_path_candidate_success'), 3)} vs {rng(col('fast_path_baseline_success'), 3)}",
                ],
                [
                    "cheaper-but-worse candidate: total cost vs baseline (simulated)",
                    f"${st.median(col('fast_path_candidate_total_cost')):.5f} vs ${st.median(col('fast_path_baseline_total_cost')):.5f}",
                ],
                [
                    "cost per successful task: baseline → rule fix (simulated)",
                    f"${st.median(col('rule_fix_baseline_cost_per_success')):.6f} → ${st.median(col('rule_fix_candidate_cost_per_success')):.6f}",
                ],
                [
                    "model-style optimizer: candidates proposed / rejected by policy in code",
                    f"{r0['model_candidates']} / {r0['model_candidates_rejected_by_policy']}",
                ],
                [
                    "failure detected → eligible decision (wall time, rule path)",
                    f"{rng(col('wall_failure_to_eligible_s'), 2)} s",
                ],
                [
                    "diagnosis + optimizer simulated spend",
                    f"${st.median(col('optimizer_diagnosis_simulated_cost_usd')):.6f}",
                ],
            ],
        )
        + "\n\nThe optimization overhead is dominated by running the two experiment arms (real executions), not by proposing candidates: the rule engine proposes in milliseconds and calls no model. "
        "A candidate that is cheaper but worse is refused because its per-case regressions and quality gates fail, whatever its cost."
    )


def main() -> None:
    meta = json.loads((RES / "meta.json").read_text())
    m = meta["machine"]
    head = (
        "## Platform benchmarks (Phases 5-10)\n\n"
        f"Machine: {m['cpu']}, {m['cpu_count']} cores, {m['memory_gb']} GB, {m['platform']}, Python {m['python']}, {m['postgres']} in Docker. "
        f"Started {meta['started_at']}; sections took "
        + ", ".join(f"{k} {v['seconds']:.0f} s" for k, v in meta["sections"].items())
        + ". "
        "Real worker processes and an isolated database; run from a frozen `git worktree` with the app containers stopped; deterministic offline providers (no model calls). "
        "Raw CSVs: `benchmarks/results/platform/`."
    )
    FRAG.mkdir(parents=True, exist_ok=True)
    (FRAG / "30_platform.md").write_text(
        "\n\n".join([head, memory(), security(), deployment(), optimization()]) + "\n"
    )
    print("wrote", FRAG / "30_platform.md")


if __name__ == "__main__":
    main()
