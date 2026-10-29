# AgentOS Autopilot: known limitations

Autopilot is a portfolio-grade engineering system, **not production-ready**. This file
lists what is not done, not verified, or weaker than it may look.

## Not verified

* **Autopilot ran on a real model only once, on a small scale.** On 2026-10-28 the Diagnosis Agent, the Optimizer
  and an extraction agent ran on Claude Haiku 5.5 (1 trial, 9 to 16 cases; see the README's Real-model results and
  `benchmarks/results/real_haiku/autopilot.json`). It produced a hypothesis and 3 candidates (2 rejected by policy)
  and the one evaluated candidate was worse and refused. Every other Autopilot result, including all benchmarks, is
  from the deterministic mock (`mock-diagnosis`, `mock-optimizer`, `mock-extractor`). OpenAI was never run, and the
  opt-in `test_live_autopilot.py` has not been run (the Haiku run used `scripts/real_haiku_autopilot.py` instead).
* **The demo results come from scripted agents.** The quality improvements in the demos and
  benchmarks are real measurements of real runs, but the agent's behavior is a scripted
  function of its config. They show that the pipeline measures, gates and rolls back
  correctly. They say nothing about how often a real LLM optimizer finds real improvements.
* **CI has not run on GitHub.** The workflow (including the new Autopilot demo step) was
  only run locally, step by step.

## Methodology limits

* **The benchmark is small.** It has one dataset family (numeric extraction): 9
  development, 16 validation and 8 test cases. n = 16 is enough to separate the large mock
  effects but would leave the CI wide for realistic, smaller effects. The `sample_size` gate
  and the "inconclusive" wording exist for that reason.
* **The bootstrap treats cases as exchangeable,** and trials are averaged per case. It does
  not model case difficulty strata or correlated cases. McNemar dichotomizes per-case success
  at 0.5.
* **Repeated experiments on the same validation split** increase the chance of overfitting it
  (adaptive data analysis). Nothing limits how many candidates a user evaluates against one
  split. The held-out test split mitigates but does not prevent this: nothing stops a user
  from evaluating many candidates on it too.
* **The LLM judge is advisory and has no calibration workflow in the UI.** Its calibration
  examples must be supplied through the dataset JSON.
* **Leakage detection is heuristic.** It catches hidden expected values (numbers normalized,
  strings ≥ 4 characters) appearing in a candidate, but not paraphrased or encoded answers.
  The real protection is architectural: the optimizer never receives hidden expected outputs.
* **The safety-weakening check is a regex.** It is a guard rail, not a guarantee. It sits on
  top of the hard restrictions that do not depend on wording: field allow-list, tools may
  only shrink, model allow-list, immutable limits, and runtime tool policy.

## Deployment and monitoring

* **Monitoring is a simple failure-rate comparison:**
  * at least `min_live_samples` runs (default 5)
  * an absolute increase of at least 0.25
  * no sequential testing and no latency or cost regression detection

  With small samples it can miss regressions or flag noise. It never acts on its own unless
  `auto_rollback` is enabled (off by default and not exercised by the demos).
* **Canary** routing is a deterministic hash bucket per run. It is covered by tests but not
  by a demo, and there is no automatic canary-to-full promotion.
* **Rollback affects new runs only.** Runs already pinned to the bad version keep it until
  they finish (by design, for reproducibility). Approval expiry is checked when someone
  decides, not by a background sweeper, so an expired request stays `pending` until it is
  touched.
* **Approval is a single administrator.** There is no two-person rule, and an admin can
  approve their own request.

## Scope

* Autopilot optimizes **agent configuration only**: instructions, tool descriptions, tool
  strategy, model (within an allow-list), effort/max_tokens, step and retry limits. It does
  not change workflows, tools, code or evaluation data.
* Evaluation runs reuse the main worker pool, so heavy evaluations compete with production
  runs. There is no separate queue or priority.
* Autopilot events and failure signals have no retention or partitioning policy.
* Development authentication from the base system is unchanged (seeded users, HS256 JWT in
  `localStorage`).
