## Platform benchmarks (Phases 5-10)

Machine: Apple M1, 8 cores, 8.0 GB, macOS-26.5.2-arm64-arm-64bit-Mach-O, Python 3.13.7, postgres (PostgreSQL) 16.15 in Docker. Started 2026-10-26T23:25:36-0700; sections took memory 223 s, security 42 s, deployment 108 s, optimization 87 s. Real worker processes and an isolated database; run from a frozen `git worktree` with the app containers stopped; deterministic offline providers (no model calls). Raw CSVs: `benchmarks/results/platform/`.

## Memory Lab: strategies under fact churn

![memory](platform_memory_accuracy.png)

Each cell is 3 seeded scenarios (seeds 1, 2, 3); every (strategy, question) is a graded workflow run on real workers. The reader is a scripted policy with fixed matching rules, so this measures how each strategy's *context* affects it, not how a real model uses memory. In the chart the persistent, refreshed-summary and retrieval lines overlap exactly (same accuracy at every churn level); no-memory and short-term rise with churn only because more questions have the correct answer *unknown*. Churn levels (facts updated, facts expired): (0,0), (4,2), (8,3), (12,5). Reproduce: `cd benchmarks && uv run --group bench python platform_bench.py memory --label platform`.

### Churn: 0 updated, 0 expired

| strategy | accuracy | stale answers (of N) | wrong-entity reuse (of 4) | retrieval precision | answer recall | context chars (mean) |
|---|---|---|---|---|---|---|
| no memory | 0.211 ± 0.000 | 0 / 0 | 0 | n/a | 0.000 ± 0.000 | 0 |
| short-term | 0.316 ± 0.000 | 0 / 0 | 0 | n/a | 0.000 ± 0.000 | 5 |
| persistent (all) | 0.895 ± 0.000 | 0 / 0 | 4 | 0.038 ± 0.000 | 1.000 ± 0.000 | 586 |
| summaries (stale) | 0.895 ± 0.000 | 0 / 0 | 4 | 0.100 ± 0.000 | 1.000 ± 0.000 | 612 |
| summaries (refreshed) | 0.895 ± 0.000 | 0 / 0 | 4 | 0.100 ± 0.000 | 1.000 ± 0.000 | 612 |
| retrieval top-3 | 0.895 ± 0.000 | 0 / 0 | 4 | 0.333 ± 0.000 | 1.000 ± 0.000 | 74 |

### Churn: 4 updated, 2 expired

| strategy | accuracy | stale answers (of N) | wrong-entity reuse (of 4) | retrieval precision | answer recall | context chars (mean) |
|---|---|---|---|---|---|---|
| no memory | 0.263 ± 0.000 | 0 / 6 | 0 | n/a | 0.000 ± 0.000 | 0 |
| short-term | 0.368 ± 0.000 | 0 / 6 | 0 | n/a | 0.000 ± 0.000 | 5 |
| persistent (all) | 0.904 ± 0.015 | 0 / 6 | 4 | 0.042 ± 0.000 | 1.000 ± 0.000 | 540 |
| summaries (stale) | 0.737 ± 0.000 | 6 / 6 | 4 | 0.100 ± 0.000 | 1.000 ± 0.000 | 612 |
| summaries (refreshed) | 0.904 ± 0.015 | 0 / 6 | 4 | 0.100 ± 0.000 | 1.000 ± 0.000 | 579 |
| retrieval top-3 | 0.904 ± 0.015 | 0 / 6 | 4 | 0.333 ± 0.000 | 1.000 ± 0.000 | 73 |

### Churn: 8 updated, 3 expired

| strategy | accuracy | stale answers (of N) | wrong-entity reuse (of 4) | retrieval precision | answer recall | context chars (mean) |
|---|---|---|---|---|---|---|
| no memory | 0.289 ± 0.000 | 0 / 11 | 0 | n/a | 0.000 ± 0.000 | 0 |
| short-term | 0.395 ± 0.000 | 0 / 11 | 0 | n/a | 0.000 ± 0.000 | 5 |
| persistent (all) | 0.895 ± 0.000 | 0 / 11 | 4 | 0.043 ± 0.000 | 1.000 ± 0.000 | 521 |
| summaries (stale) | 0.605 ± 0.000 | 11 / 11 | 4 | 0.100 ± 0.000 | 1.000 ± 0.000 | 612 |
| summaries (refreshed) | 0.895 ± 0.000 | 0 / 11 | 4 | 0.100 ± 0.000 | 1.000 ± 0.000 | 565 |
| retrieval top-3 | 0.895 ± 0.000 | 0 / 11 | 4 | 0.333 ± 0.000 | 1.000 ± 0.000 | 71 |

### Churn: 12 updated, 5 expired

| strategy | accuracy | stale answers (of N) | wrong-entity reuse (of 4) | retrieval precision | answer recall | context chars (mean) |
|---|---|---|---|---|---|---|
| no memory | 0.342 ± 0.000 | 0 / 17 | 0 | n/a | 0.000 ± 0.000 | 0 |
| short-term | 0.447 ± 0.000 | 0 / 17 | 0 | n/a | 0.000 ± 0.000 | 5 |
| persistent (all) | 0.921 ± 0.026 | 0 / 17 | 3 | 0.048 ± 0.000 | 1.000 ± 0.000 | 473 |
| summaries (stale) | 0.447 ± 0.000 | 17 / 17 | 4 | 0.100 ± 0.000 | 1.000 ± 0.000 | 612 |
| summaries (refreshed) | 0.921 ± 0.026 | 0 / 17 | 3 | 0.100 ± 0.000 | 1.000 ± 0.000 | 528 |
| retrieval top-3 | 0.921 ± 0.026 | 0 / 17 | 3 | 0.333 ± 0.000 | 1.000 ± 0.000 | 71 |

Findings the data supports: stale-summary errors scale with churn while current-state strategies have none; every strategy that stores the sibling entity reuses it for the look-alike questions (memory does not always help); retrieval matches full-context accuracy with a fraction of the context. Variation across seeds is small because the scenario *structure* is fixed by the churn level; what varies by seed is which facts change.

## Security Center: enforcement under a labeled attack corpus

5 full evaluations on an isolated database (real workflows on real workers, a deliberately gullible mock model that obeys every injected directive). Reproduce: `cd benchmarks && uv run --group bench python platform_bench.py security --label platform`.

| measure | median over runs | min-max over runs |
|---|---|---|
| policy tests passed | 11/11 | 11-11 |
| injection attacks contained | 28/28 | 28-28 |
| benign controls not blocked | 8/8 | 8-8 |
| trace secret-exposure checks passed | 4/4 | 4-4 |

| metric | value (every run) |
|---|---|
| attacks actually attempted | 28/28 in 5/5 runs |
| unauthorized actions attempted / blocked | 50 / 34 |
| attacks with no harmful effect | 28/28 |
| unsafe side effects | 0 (max over runs) |
| false-positive blocks (benign controls) | 0/8 (max over runs) |
| cross-workspace canary leaks | 0 (max over runs) |
| planted secrets found in persisted traces | 0/4 (max over runs) |
| evaluation wall time | 7.7 (7.7-8.7) s |

This measures the platform's code-level enforcement against a fixed corpus of 28 attacks (7 goals x 4 surfaces) and 8 benign controls. It is **not** evidence that a real model resists prompt injection, and a corpus this small is a regression floor, not a guarantee.

## Release Center: routing, rollback rules and version consistency

![routing](platform_routing.png)

**Canary routing correctness.** `canary_bucket` over 50000 random run ids per seed, 8 target percentages, 4 seeds: the achieved share is within the 95% binomial band in 28/32 cells (about 95% expected by construction), the largest absolute error is 0.744 percentage points, routing is deterministic for a given (run, agent, seed), and the sets are nested (a run routed at p% is routed at any larger p) in 32/32 cells.

![rollback](platform_rollback.png)

**Rollback rules: operating characteristics.** The real `failure_rate_increase` and `error_budget_exhausted` rule code (default thresholds) run on seeded synthetic arms (2000 trials per cell); this characterizes the configured rules, not any real workload. 'either' = a rollback is triggered by either rule.

| steps per arm | canary failure rate | rollback (either rule) | failure-rate rule | error-budget rule (95% SLO) | insufficient evidence | case |
|---|---|---|---|---|---|---|
| 10 | 5% | 0% | 0% | 0% | 6% | healthy (false rollback) |
| 10 | 10% | 1% | 1% | 0% | 18% | regression |
| 10 | 20% | 8% | 8% | 0% | 40% | regression |
| 10 | 40% | 43% | 43% | 0% | 45% | regression |
| 10 | 80% | 97% | 97% | 0% | 3% | regression |
| 20 | 5% | 2% | 0% | 2% | 3% | healthy (false rollback) |
| 20 | 10% | 13% | 2% | 13% | 15% | regression |
| 20 | 20% | 58% | 21% | 58% | 36% | regression |
| 20 | 40% | 98% | 82% | 98% | 15% | regression |
| 20 | 80% | 100% | 100% | 100% | 0% | regression |
| 40 | 5% | 4% | 0% | 4% | 0% | healthy (false rollback) |
| 40 | 10% | 37% | 7% | 37% | 0% | regression |
| 40 | 20% | 92% | 53% | 92% | 4% | regression |
| 40 | 40% | 100% | 98% | 100% | 1% | regression |
| 40 | 80% | 100% | 100% | 100% | 0% | regression |
| 80 | 5% | 5% | 0% | 5% | 0% | healthy (false rollback) |
| 80 | 10% | 57% | 1% | 57% | 0% | regression |
| 80 | 20% | 99% | 54% | 99% | 0% | regression |
| 80 | 40% | 100% | 100% | 100% | 0% | regression |
| 80 | 80% | 100% | 100% | 100% | 0% | regression |

The baseline fails 5%, which is also the SLO boundary of the error-budget rule, so a canary at 5% is *healthy*. **A first version of this benchmark exposed a flaw**: with a plain "failures exceed the budget" test, a healthy canary was rolled back 28-37% of the time at 20+ steps per arm. Both rules now also require statistical evidence (one-sided Fisher exact test for the difference between arms, one-sided binomial test against the SLO boundary, alpha 0.05); the table above is from the fixed code, where false rollbacks stay at about the alpha level or below and small samples report `insufficient evidence` instead of acting on noise.

**Live releases** (5 repetitions: gates, admin approval, 50% seeded canary, 30 live runs through 3 real workers, automatic rollback by the coordinator, then 10 post-rollback runs):

| measure | result |
|---|---|
| automatic rollback triggered | 5/5 |
| canary-arm failures (at the step limit) | 77/77 canary runs |
| rollback decided after last live run finished (median / min-max) | 0.44 (0.00-0.55) s |
| runs pinned to a version different from the seeded routing decision | 0/160 |
| active/canary pointers consistent after rollback | 5/5 |
| post-rollback runs succeeded on the restored version | 10/10, 10/10, 10/10, 10/10, 10/10 |

The detection time above is dominated by the coordinator tick interval, not by the rule computation. The canary workload fails deterministically (the step limit), so this shows the mechanism end to end, not detection of subtle regressions (see the operating characteristics for those).

## Optimization Lab: baseline vs candidate on held-out cases

![optimization](platform_optimization.png)

5 full cycles on fresh isolated databases (flawed baseline → failure detection → diagnosis → rule-engine candidate and the model-style optimizer's candidates → paired experiments on the 16-case held-out validation split). Mock provider: quality numbers describe the scripted demo agent. Reproduce: `cd benchmarks && uv run --group bench python platform_bench.py optimization --label platform`.

| measure | result (identical across repetitions unless a range is shown) |
|---|---|
| baseline success (development / validation) | 0.556 (0.556-0.556) / 0.438 (0.438-0.438) |
| rule-engine candidate: success on validation | 1.000 (1.000-1.000) |
| rule-engine candidate: decision | eligible |
| rule-engine candidate: 95% CI lower bound of the paired difference | 0.312 (0.312-0.312) |
| rule-engine candidate: per-case regressions | 0 |
| cheaper-but-worse candidate: decision / per-case regressions detected | not_eligible / 7 (7-7) |
| cheaper-but-worse candidate: success vs baseline | 0.000 (0.000-0.000) vs 0.438 (0.438-0.438) |
| cheaper-but-worse candidate: total cost vs baseline (simulated) | $0.00870 vs $0.04895 |
| cost per successful task: baseline → rule fix (simulated) | $0.006994 → $0.002146 |
| model-style optimizer: candidates proposed / rejected by policy in code | 3 / 1 |
| failure detected → eligible decision (wall time, rule path) | 6.07 (6.01-6.10) s |
| diagnosis + optimizer simulated spend | $0.005608 |

The optimization overhead is dominated by running the two experiment arms (real executions), not by proposing candidates: the rule engine proposes in milliseconds and calls no model. A candidate that is cheaper but worse is refused because its per-case regressions and quality gates fail, whatever its cost.
