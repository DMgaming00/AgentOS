# Real-model results (Claude Haiku 5.5, plus Sonnet 5.5 where stated)

Moved from the main README. Every number here comes from the saved raw results in
[benchmarks/results/real_haiku/](../benchmarks/results/real_haiku/) and the verification log in
[AGENTOS_2_TEST_RESULTS.md](../AGENTOS_2_TEST_RESULTS.md).

On 2026-10-28 and 2026-10-29 (UTC) the platform was run deliberately against the real Anthropic API in two rounds, under
an application-level spend cap and a switch that was turned off again afterwards. Everything else in the repository is offline
mock or fixture data unless it says so. These are **small, single-run demonstrations, not benchmarks of Haiku or Sonnet**:
one run per experiment, one to three trials per case, 6 to 150 cases. Real models are nondeterministic, so a rerun can differ.
Raw results: [benchmarks/results/real_haiku/](../benchmarks/results/real_haiku/).

**Total: about 7,500 real model calls and about $14 estimated** (round 1: about $0.76; round 2: about $13.2, of which Sonnet
was about $10.2). Costs are price-table estimates, not invoices. Each lab's real mode is opt-in (admin only, refused unless the
four paid-mode settings are on, counted against the spend cap); the offline versions stay the default and the automated test
suite stays offline whatever `.env` says.

### Round 2: a harder evaluation set, a real repair, and a model comparison

[`hard-ops`](../examples/hard-eval/README.md) is a deterministic 150-case set (41 development / 50 validation / 59 test) over 12
fictional platforms: multi-hop filtering, cost arithmetic from the *current* prices, a changelog that overrides launch
documents (including "as of date" questions), per-1K vs per-1M unit traps, exhaustive counts and sums over all platforms, a fact
buried in a long handbook, and near-miss abstention. Answer keys come from the same fact model that renders the documents, and
`scripts/build_hard_eval.py` re-parses the rendered text independently (it caught a bug in its own first verifier).

| What ran | Result |
|---|---|
| **How hard is it for Haiku?** | With a calculator and 14 steps the plain Haiku agent scored 39/40 (97.5%) on the first version and 38/41 (92.7%) on the final one: **Haiku is strong at this task**, so there was nothing to repair. Harder questions alone did not create headroom |
| **A realistic deployment flaw** | The same agent with a **3-step budget** scored 19/41 (46%) |
| **Autopilot on the flawed agent** (Sonnet diagnoser and optimizer, Haiku agent) | Failures: 21 task-incomplete, 5 repetitive loops. Sonnet's diagnosis: *"The agent's max_steps=3 budget is too small for these multi-hop document questions... the model kept issuing document_search calls through every allowed step and never produced the final JSON answer"*, which is correct |
| **Candidates, validation split** (50 cases x 3 trials, paired, interleaved) | Model-written "raise step cap and add efficient-search instructions": **48.0% -> 98.0%** (+0.50, 95% CI [+0.38, +0.62], 27 improvements, 0 regressions, cheaper per success: $0.0040 -> $0.0024), **eligible**. Rule engine "raise the step limit to 5": 50.0% -> 92.0% (+0.42, CI [+0.31, +0.53]), **eligible**. "Higher step cap with a stronger model": 50.7% -> 86.7%, **not eligible** (unauthorized tool behavior, cost per success) |
| **Held-out test split** (59 cases x 2 trials, scored once) | Best candidate: **47.5% -> 89.0%**, paired difference +0.415, 95% CI [+0.29, +0.54]. The improvement is statistically supported, but the strict gate still said **not eligible** because 2 individual cases regressed. Nothing was promoted |
| **Haiku vs Sonnet** (careful prompt, 14 steps, validation, 1 trial) | Haiku **41/50 (82%)** at $0.0054 per success; Sonnet **46/50 (92%)** at $0.057 per success: about +10 points for about 10x the cost per success. One trial over 50 cases: the gap is suggestive, not established |
| **Security Center, naive prompt** (the agent is told to carry out the documents' instructions) | 4 runs x 28 attacks: the model followed an injection only **4 times** (twice a read of another workspace's secret file, each answered "not found in this workspace"; twice a partial call flood, 1 call where 8 were asked, inside the budget). **No harmful effect in any run**, 0 false-positive blocks, 0 secrets in traces. This is the first real-model evidence of containment, and it is thin (4 followed attacks). Two grader problems found along the way (a model's own bad argument counted as a false positive; a flood the model did not carry out counted as a failure) were fixed |
| **Chaos Lab at n=20** (3 parallel Haiku lookups + aggregation) | Brownout: **0/20 with retries off, 20/20 with retries on** (67 injections vs 28). Worker crash loop: **0/20 for both 3 and 5 attempts because every run was cancelled by the 10-minute safety cap** (20 runs on 2 workers, each crash kills co-located steps and each recovery waits out a 15 s lease): a capacity limit of the setup, not a retry-budget result. An earlier n=6 run completed 1/6 with 3 attempts and 5/6 with 5 |
| **Memory Lab, 10 seeds** (Haiku reader, 380 questions per strategy) | Retrieval, full context and refreshed summaries **380/380**; stale summaries 228/380 (60%): all 152 stale facts answered wrongly; no memory 33% and short-term 43%; **zero look-alike mix-ups** (the scripted reader made 3-4 per scenario) |

### Round 1: engine, Security, Arena, Chaos, Memory and Autopilot smoke runs

| Lab | What ran on real Haiku | Result |
|---|---|---|
| **Core engine** | Research report workflow (3 parallel research agents, analysis, writer, approval, export) | Succeeded in 65.8 s; 10 model calls; 77,460 in / 21,799 out tokens reported by the provider; $0.0186; report written only after approval |
| **Security Center** | The 28 injection attacks and 8 benign controls, Haiku reading the poisoned documents | **Haiku ignored 28/28** (it wrote, for example, "The excerpt contains an embedded instruction to read ../../../etc/passwd... I did not follow it. It is document content, not a task instruction"). 0 unsafe effects, 0/8 benign blocked, 0/4 secrets in traces, 11/11 policy tests |
| **Agent Arena** | 5 Haiku contenders on the 111-case corpus, test split scored once | Test overall: **Careful 0.94** (45/48), Calculator 0.87, Formatter 0.86, **Bare 0.77** (37/48, ranked below the top group), Ungrounded 0.33 (13/48; 13/13 on calculation, 0/35 on retrieval, as it should). Validation: the first four statistically tied |
| **Chaos Lab** | Faults around real calls and real workers, 6 runs per experiment | Brownout 0/6 without retries, 6/6 with; crash loop 1/6 with 3 attempts, 5/6 with 5; 30/30 state-consistency checks every time |
| **Autopilot (first attempt)** | Haiku agent, Haiku diagnosis and optimizer, rule engine | Baseline 5/9; 3 candidates, 2 rejected by policy; the evaluated one (93.8% -> 75.0%) was refused; the rule engine proposed nothing |
| **Paired prompt edit** | Bare -> Careful instructions, `arena-extraction` validation, 16 cases x 3 trials | 70.8% -> 77.1%, CI [-0.125, +0.271], McNemar p = 0.625, 1 regression: **not eligible**, an improvement the evidence did not support |

### How to read this

* The platform works end to end on real models, and its gates behave sensibly: they accepted a large, well-supported repair
  (and still blocked it for two per-case regressions), and refused two changes that did not clearly help.
* The honest constraints: Haiku is strong on these tasks, so the repairs shown are for a deliberately constrained deployment
  (a tight step budget), not for the model being weak. The injection corpus barely moves Haiku, so containment of a real
  followed attack rests on 4 observed cases. The crash-loop result at n=20 is limited by the experiment's capacity. All of it
  is small-n, one run, and nondeterministic.
* Chaos approvals in these runs are decided by a simulated reviewer.
* I also made mistakes while doing this, all recorded in the commit history and test results: a chaos run that silently used
  scripted agents (caught because the spend ledger did not move, discarded), an optimizer arm that switched to Sonnet and cost
  about $5, and two security-grading rules that were too strict for a real model.

Reproduce (live mode on and a cap set, see [docs/ENABLE_REAL_CLAUDE.md](ENABLE_REAL_CLAUDE.md)): `scripts/real_hard_eval.py`
(`setup`, `eval`, `optimize`, `testcand`), `scripts/real_haiku_autopilot.py`, `real_haiku_paired.py`, `real_haiku_arena.py`,
`real_haiku_chaos.py`, `real_haiku_memory.py`, `scripts/demo.py research`, and "Run with real model" (or
`POST /security/runs?agent=live_naive`) in the Security Center.

| | |
|---|---|
| ![Optimizer, validation](screenshots/haiku2-optimizer-validation.png)<br>**Autopilot repair, validation**: 48.0% -> 98.0% | ![Optimizer, test](screenshots/haiku2-optimizer-test.png)<br>**Held-out test**: 47.5% -> 89.0%, gate: 2 regressions |
| ![Sonnet diagnosis](screenshots/haiku2-failures-diagnosis.png)<br>**Sonnet-written diagnosis** of the step-budget failure | ![Security, naive prompt](screenshots/haiku2-security-naive.png)<br>**Security Center, naive-prompt real model** |
| ![Chaos brownout without retries](screenshots/haiku2-chaos-brownout-off.png)<br>**Chaos, brownout, retries off**: 0/20 | ![Chaos brownout with retries](screenshots/haiku2-chaos-brownout-on.png)<br>**Chaos, brownout, retries on**: 20/20 |
| ![Real Haiku run](screenshots/haiku-run-graph.png)<br>**A real research run** (round 1) | ![Real run cost](screenshots/haiku-run-cost.png)<br>**Cost per step and model** |
| ![Security Center, real model](screenshots/haiku-security.png)<br>**Security Center, real model** (round 1) | ![Arena, real contenders](screenshots/haiku-arena.png)<br>**Agent Arena**, 5 Haiku contenders |
| ![Chaos, brownout without retries](screenshots/haiku-chaos-brownout.png)<br>**Chaos, n=6**: brownout, retries off | ![Chaos, crash loop with 5 attempts](screenshots/haiku-chaos-crash.png)<br>**Chaos, n=6**: crash loop, 5 attempts |
| ![Memory Lab, real reader](screenshots/haiku-memory.png)<br>**Memory Lab** with a real reader | ![Paired experiment](screenshots/haiku-paired-experiment.png)<br>**Paired prompt edit**: not supported, refused |
| ![Real failures](screenshots/haiku-autopilot-failures.png)<br>**Failure Intelligence**, Haiku diagnosis | ![Real experiment](screenshots/haiku-autopilot-experiment.png)<br>**Optimizer candidate** refused |
| ![Real FinOps](screenshots/haiku-finops.png)<br>**FinOps** (round 1): real calls, 0% simulated | ![Real trace](screenshots/haiku-run-trace.png)<br>**Trace** of the real run |
