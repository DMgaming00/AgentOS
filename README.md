# AgentOS

### Build, break, measure and safely ship AI agents, offline by default and proven on real Claude.

AgentOS is an **agent-engineering platform**: durable multi-worker execution, a flight recorder, chaos testing, security
enforcement, evaluation and an agent arena, memory strategies, cost control, and gated canary releases with automatic
rollback. Agent runs survive worker crashes, tool permissions are enforced in code (not in prompts), sensitive actions wait for
human approval, and every attempt, model call, tool call and dollar is inspectable. **Autopilot** diagnoses an agent's failures
from trace evidence, proposes fixes, tests them on held-out cases with real statistics, and only then lets a human promote them.

<p align="center">
  <a href="https://github.com/DMgaming00/AgentOS/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/DMgaming00/AgentOS/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="offline by default" src="https://img.shields.io/badge/paid%20model%20calls-blocked%20by%20default-2ea44f">
  <img alt="real claude" src="https://img.shields.io/badge/verified%20on-real%20Claude%20Haiku%20%2B%20Sonnet-d97706">
  <img alt="tests" src="https://img.shields.io/badge/tests-435%20backend%20%C2%B7%2027%20e2e-2563eb">
  <img alt="python" src="https://img.shields.io/badge/python-3.13-3776ab">
  <img alt="postgres" src="https://img.shields.io/badge/PostgreSQL-16-336791">
  <img alt="next" src="https://img.shields.io/badge/Next.js-16-000000">
  <img alt="mypy" src="https://img.shields.io/badge/mypy-strict-blue">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-lightgrey">
</p>

![Run waiting for approval](docs/screenshots/run-waiting-approval.png)

> **Zero-cost and offline by default.** No paid model is called unless four settings are all changed deliberately, enforced in
> code at two layers (not just documented), and a spend cap is checked before every request. No API keys are needed for the app,
> the demos, the benchmarks, the tests or CI. Unless a section says "real Claude", numbers come from deterministic mock or
> fixture providers and describe *platform behavior*, not the intelligence of any real model.
>
> A production-inspired portfolio project, not a production system. See [Known limitations](#known-limitations).

## Headline results (real Claude, small-n, honestly labeled)

![Real-model headline results](docs/benchmarks/real_results.png)

| Claim | Evidence | Where |
|---|---|---|
| **Autopilot repairs a flawed real agent and the repair holds up out of sample** | 3-step-budget Haiku agent: 48% -> 98% on validation (CI +0.38 to +0.62), **47.5% -> 89.0% on a held-out test** (CI +0.29 to +0.54). Sonnet's diagnosis named the cause correctly | [Round 2](#round-2-a-harder-evaluation-set-a-real-repair-and-a-model-comparison) |
| **Gates refuse what the evidence does not support** | A prompt edit that "looked" better (70.8% -> 77.1%) was refused (CI includes 0); the strong repair above was still blocked by 2 per-case regressions | [Round 1](#round-1-engine-security-arena-chaos-memory-and-autopilot-smoke-runs) |
| **Retries and recovery are measurable** | Provider brownout: **0/20 -> 20/20** completed with retries; worker crashes: 1/6 -> 5/6 with a larger retry budget; 30/30 state-consistency checks every time | Chaos Lab |
| **Security enforcement under real-model attack** | 0 harmful effects over 140 attack cases (28 attacks x 5 real-model runs); the model followed 4 injections under a "naive" prompt and **all 4 were contained** | Security Center |
| **Memory strategies compared on a real reader** | Retrieval = full context = refreshed summaries = **380/380**; stale summaries only 60% (every stale fact answered wrongly) | Memory Lab |
| **Model choice has a price** | Sonnet 92% vs Haiku 82% on the same prompt at about **10x the cost per success** | Agent Arena / Evaluations |
| **A real benchmark of the platform itself** | Old-vs-new engine control (ratio 0.90-1.07), 18/18 crash recoveries, a rollback rule whose 28-37% false-positive rate was found by the benchmark and fixed | [Benchmarks](#benchmarks) |

Everything is reproducible from scripts and saved raw results ([benchmarks/results/real_haiku/](benchmarks/results/real_haiku/)),
total real spend was about **$14**, and the README says where each result is thin.

### What is proven, and what is not

| | Status |
|---|---|
| Durable execution: crash-safe leases, fencing, idempotent side effects, approvals bound to exact arguments | Proven with real killed processes |
| Offline guarantee (no paid call without four flags and a spend cap, two enforcement layers, tests cannot spend) | Proven; verified while a live `.env` was present |
| Autopilot loop end to end on a real model, with gates | Proven at small n; repairs a *constrained* agent, not a weak model |
| Containment of a real model following an injection | **Thin**: 4 observed followed attacks, all contained |
| Chaos crash-loop recovery at n=20 | **Not shown**: runs hit the 10-minute cap (capacity limit); n=6 result only |
| Real-model result for OpenAI or other providers | Not done |
| Production readiness, load, third-party security review | **Not claimed** |

## Contents
[The problem](#the-problem) ·
[What makes it different](#what-makes-it-different) ·
[2.0 engineering labs](#agentos-20-engineering-labs) ·
[Five demos](#five-demonstrations) ·
[Real-model results](#real-model-results-claude-haiku-55-plus-sonnet-55-where-stated) ·
[Visual tour](#visual-tour) ·
[Architecture](#architecture) ·
[Quick start](#quick-start) ·
[Benchmarks](#benchmarks) ·
[Testing](#testing) ·
[Security](#security-model) ·
[Lessons](#engineering-lessons-from-building-20) ·
[Limitations](#known-limitations) ·
[Roadmap](#roadmap)

**Documentation map:** [architecture](AGENTOS_2_ARCHITECTURE.md) · [status](AGENTOS_2_IMPLEMENTATION_STATUS.md) ·
[test results](AGENTOS_2_TEST_RESULTS.md) · [benchmarks](AGENTOS_2_BENCHMARKS.md) · [limitations](AGENTOS_2_KNOWN_LIMITATIONS.md) ·
[enable real Claude](docs/ENABLE_REAL_CLAUDE.md) · [hard evaluation set](examples/hard-eval/README.md) · [demos](docs/demos/README.md)

### 60-second start

```bash
git clone https://github.com/DMgaming00/AgentOS.git && cd AgentOS
make up                       # postgres, redis, migrations + seed, api, 2 workers, web
open http://localhost:3000    # admin@agentos.dev / agentos-admin
make showcase                 # five end-to-end demos, no API keys, no network model calls
```


## The problem

Most agent frameworks run an LLM tool loop inside one process. When that process
dies mid-run, work is lost or silently repeated. When a model asks for a dangerous
tool, the only guard is the prompt. When something goes wrong, there's no record of
which attempt did what. AgentOS treats agent workflows like any other distributed
job system. State lives in PostgreSQL, work is leased and fenced, side effects are
idempotent or approval-gated, and the model is never trusted to authorize anything.

## What makes it different

* **Crash-safe scheduling.** Steps are claimed with `SELECT … FOR UPDATE SKIP LOCKED`
  under per-claim lease tokens. Workers heartbeat. When a worker dies, its lease
  expires and another worker resumes the step from a durable conversation checkpoint.
* **Fencing.** Every completion, failure, checkpoint and side-effecting write checks
  the current lease token, so a late worker can never overwrite a newer result. This
  is tested with real processes being killed.
* **Policy in code, not prompts.** Effective tools are the agent's allow-list ∩ the
  node policy ∩ the registry. AI-generated plans go through the same validator as
  hand-written DAGs. A document that tells the model to exfiltrate secrets gets the
  call *denied*; a test proves it.
* **Human approval bound to exact arguments.** Approvals are tied to a tool execution
  and the SHA-256 of its canonical arguments, are single-use, and expire. A rejected
  action never executes.
* **Honest accounting.** Token usage comes from provider responses (unknown stays
  `NULL`, never 0). Costs come from a versioned price table and are labeled
  `actual / partial / simulated / unknown`. Budgets are reserved atomically before
  each call.
* **Measured, not claimed.** Benchmarks run real worker processes and compute every
  number from persisted timestamps, including one optimization with an A/B.

## Autopilot: evaluation-driven agent improvement

Autopilot proposes changes to an agent's configuration, measures them rigorously, rejects
regressions, and deploys improvements only with human approval:

**Run → Observe → Diagnose → Propose → Evaluate → Compare → Approve → Deploy → Monitor → (Rollback)**

* **Immutable agent versions.** Every config is a content-addressed version, and every step
  records the version it ran. Deployments and rollbacks affect new runs only.
* **Failure intelligence.** Deterministic detectors classify failures (infrastructure vs
  agent). An LLM Diagnosis Agent proposes a *hypothesis* that must cite evidence ids from a
  sanitized evidence pack, or it is stored as `invalid`.
* **Constrained optimizer.** It generates 2–5 candidates. Restrictions are enforced in code:
  * only approved fields can change, and tools can only be removed;
  * the model must be on an allow-list, and budgets and timeouts are immutable;
  * changes that weaken safety or leak hidden answers are rejected.
* **OpenEval** (`packages/openeval`, standalone). Versioned dev/validation/test datasets,
  deterministic graders (schema, exact, numeric tolerance, tool correctness, citations that
  must match retrieved sources), metrics with denominators, and an advisory-only LLM judge.
* **Paired experiments.** Baseline and candidate run the same cases, interleaved, with
  repeated trials, a bootstrap 95% CI and McNemar. Nothing is called significant unless the
  CI excludes 0 with enough cases.
* **Gates, then an admin.** Per-case regressions, non-inferiority, unauthorized tool
  behavior and cost per *successful* task are checked before an administrator approves.
  Live monitoring flags regressions, and rollback is one audited action.

| | |
|---|---|
| ![Experiment: eligible](docs/screenshots/autopilot-experiment-eligible.png) | ![Experiment: cheaper but worse, rejected](docs/screenshots/autopilot-experiment-rejected.png) |
| ![Improvements](docs/screenshots/autopilot-improvements.png) | ![Deployments and rollback](docs/screenshots/autopilot-deployments.png) |

```bash
make up && make autopilot-demo   # repair a flawed agent · reject a fake improvement · roll back a live regression
make autopilot-bench             # full cycle on an isolated database -> docs/autopilot-benchmarks.md
```

On the mock provider, the demo agent's validation success went from 43.8% to 100%
(paired difference +0.56, 95% CI [+0.31, +0.81], n=16). The held-out test went from 37.5%
to 100%. A 5.6× cheaper "fast path" candidate was refused (0% success, 7 per-case
regressions), and a step cap that passed offline gates but failed 10/10 live runs was rolled
back. These are measurements of a **scripted mock agent**, not of a real model. Docs:
[architecture](AUTOPILOT_ARCHITECTURE.md) · [status](AUTOPILOT_IMPLEMENTATION_STATUS.md) ·
[test results](AUTOPILOT_TEST_RESULTS.md) · [limitations](AUTOPILOT_KNOWN_LIMITATIONS.md) ·
[benchmark](docs/autopilot-benchmarks.md).

## AgentOS 2.0: engineering labs

Each lab is built on the same stored records (PostgreSQL rows, not logs) and the same offline execution policy.

| Lab | What it does | Evidence it was verified |
|---|---|---|
| **Engine v2** | Conditional branches and joins, bounded `fan_out`, nested `sub_workflow` with a durable parent wait, priority with aging, pause/resume, cancellation propagation | Real worker processes killed mid-step, incl. inside a child run; old-vs-new scheduler control experiment (ratio 0.90-1.07) |
| **Flight Recorder** | Immutable sanitized model-call records, frames with status deltas and integrity checks, playback UI, recorded replay vs deterministic re-execution, structured run diff, fail-closed export | Gap-free event check on every demo run |
| **Chaos Lab** | 17 faults injected at real boundaries (provider, tool, DB, worker), seeded reproducible schedules, safety caps, an injection ledger and a scorecard | Eight scenarios x 3 retry arms; found and fixed 4 platform bugs |
| **Security Center** | 11 engine-level policy tests, 28 injection attacks (7 goals x 4 surfaces) + 8 benign controls through real workflows, trace-sanitization checks, incidents | 28/28 contained, 0/8 benign blocked, 0 secrets in traces; sensitivity tests prove the suite can fail |
| **Agent Arena + OpenEval** | 111-case corpus (3 families, dev/validation/test splits, hash manifest), one-shot test split, Wilson and bootstrap intervals, leaderboard with statistical ties | Independently parsed answer keys; offline contenders by default, real contenders are an admin opt-in |
| **Memory Lab** | Versioned, audited, expiring workspace memory with provenance, untrusted marking, approval-gated writes and secret refusal; 6 strategies compared under fact churn | Stale-summary errors scale with churn (17/17 at the highest level); retrieval matches full context with ~1/7 of the characters |
| **Optimization Lab** | A deterministic, versioned rule engine (9 rules) over trace evidence proposes changes that cite failing steps; same validation, experiments and gates as model-written candidates | 43.8% -> 100% on held-out validation (CI lower bound +0.31); cheaper-but-worse candidate refused with 7 per-case regressions |
| **FinOps** | Analytics by agent/workflow/model/tool, budgets enforced *before* a call under an advisory lock, anomaly detectors, suggestions labeled measured / projected / none | Everything labeled "Estimated cost, simulated pricing" |
| **Release Center** | Development / staging / production-style environments, gated plans, seeded canary routing (5/10/25/50/100), admin-advanced stages, rollback rules with minimum evidence and significance tests, append-only audit | 5/5 automatic rollbacks, 0/160 mis-pinned runs; a benchmark found a 28-37% false-rollback bug that is now fixed |
| **Observability** | Agent Run Inspector (9 views of one run), telemetry distributions, worker health, end-to-end trace correlation (request -> run -> attempt -> model/tool call) | 22 pages checked for overflow at 390 px |

| | |
|---|---|
| ![Security Center](docs/screenshots/security.png) | ![Agent Arena](docs/screenshots/arena.png) |
| ![Memory Lab](docs/screenshots/memory-lab.png) | ![FinOps](docs/screenshots/finops.png) |
| ![Release Center](docs/screenshots/release-center.png) | ![Run inspector, trace view](docs/screenshots/run-inspector.png) |
| ![Chaos scorecard](docs/screenshots/chaos-scorecard.png) | ![Flight Recorder playback](docs/screenshots/flight-playback.png) |

### Five demonstrations

All five run through the public API against the Docker stack, print PASS/FAIL checks against stored records, and
exit non-zero on the first failure (`make showcase` runs them all; details in [docs/demos/](docs/demos/README.md)).

| Demo | Shows | Run |
|---|---|---|
| **A** Self-improving agent | A flawed agent fails 5/9 -> diagnosis with evidence ids -> rule-engine fix -> paired held-out experiment (43.8% -> 100%) -> a cheaper-but-worse candidate refused -> admin approval -> promotion | `make showcase-a` |
| **B** Chaos recovery | Seeded worker crashes and provider faults; all runs recover, state consistent, gap-free recording, generated [recovery report](docs/demos/demo_b_recovery_report.md) | `make showcase-b` |
| **C** Security enforcement | An obedient agent is told by a document to write a file; AgentOS denies it in code, records the denial and an incident; full 51-case evaluation | `make showcase-c` |
| **D** Cost optimization | Redundant tool calls found from the trace, a duplicate-call fix evaluated on held-out cases: correctness preserved, model-requested tool calls 32 -> 16, estimated cost per success -29% (simulated pricing) | `make showcase-d` |
| **E** Canary and rollback | A candidate that passes offline gates but fails live traffic; 50% seeded canary; the failure-rate rule fires with its evidence and rolls back automatically | `make showcase-e` |

### Zero-cost offline operation

```bash
make up && make showcase        # no keys, no network model calls
curl localhost:8000/api/v1/system   # execution.paid_requests_allowed: false, with the blockers listed
```

Paid requests are refused unless `AGENTOS_EXECUTION_MODE=live`, `AGENTOS_ALLOW_PAID_PROVIDERS=true`,
`AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS=true` and a positive `AGENTOS_MAX_REAL_API_SPEND_USD` are all set. A key in
the environment is never enough. The check runs at the start of every adapter call *and* under the SDK's HTTP
transport; the test suite additionally installs a session-wide guard that fails any test opening an outbound
connection. A refused call fails the run visibly; the platform never substitutes mock output for a real-provider
failure. To enable real Claude later, see [docs/ENABLE_REAL_CLAUDE.md](docs/ENABLE_REAL_CLAUDE.md).

### How the offline results should be read

* Mock and fixture providers are **scripted**: an agent's score measures how its configuration interacts with the
  scripted policy, not what a real model would do. The leaderboard and experiment pages label this.
* Costs are **simulated** from a versioned price table and labeled as estimates, never invoices.
* The benchmarks run from a frozen `git worktree`, with the app containers stopped, an isolated database and real
  worker processes; where a claim compares versions, a control experiment (same harness, only code differs) is run.
* Several benchmark findings changed the code (a rollback rule with a 28-37% false-positive rate, 4 Chaos Lab bugs,
  tracing that started new traces in workers). They are recorded rather than hidden.

## Real-model results (Claude Haiku 5.5, plus Sonnet 5.5 where stated)

On 2026-10-28 and 2026-10-29 (UTC) the platform was run deliberately against the real Anthropic API in two rounds, under
an application-level spend cap and a switch that was turned off again afterwards. Everything else in this README is offline
mock or fixture data unless it says so. These are **small, single-run demonstrations, not benchmarks of Haiku or Sonnet**:
one run per experiment, one to three trials per case, 6 to 150 cases. Real models are nondeterministic, so a rerun can differ.
Raw results: [benchmarks/results/real_haiku/](benchmarks/results/real_haiku/).

**Total: about 7,500 real model calls and about $14 estimated** (round 1: about $0.76; round 2: about $13.2, of which Sonnet
was about $10.2). Costs are price-table estimates, not invoices. Each lab's real mode is opt-in (admin only, refused unless the
four paid-mode settings are on, counted against the spend cap); the offline versions stay the default and the automated test
suite stays offline whatever `.env` says.

### Round 2: a harder evaluation set, a real repair, and a model comparison

[`hard-ops`](examples/hard-eval/README.md) is a deterministic 150-case set (41 development / 50 validation / 59 test) over 12
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

Reproduce (live mode on and a cap set, see [docs/ENABLE_REAL_CLAUDE.md](docs/ENABLE_REAL_CLAUDE.md)): `scripts/real_hard_eval.py`
(`setup`, `eval`, `optimize`, `testcand`), `scripts/real_haiku_autopilot.py`, `real_haiku_paired.py`, `real_haiku_arena.py`,
`real_haiku_chaos.py`, `real_haiku_memory.py`, `scripts/demo.py research`, and "Run with real model" (or
`POST /security/runs?agent=live_naive`) in the Security Center.

| | |
|---|---|
| ![Optimizer, validation](docs/screenshots/haiku2-optimizer-validation.png)<br>**Autopilot repair, validation**: 48.0% -> 98.0% | ![Optimizer, test](docs/screenshots/haiku2-optimizer-test.png)<br>**Held-out test**: 47.5% -> 89.0%, gate: 2 regressions |
| ![Sonnet diagnosis](docs/screenshots/haiku2-failures-diagnosis.png)<br>**Sonnet-written diagnosis** of the step-budget failure | ![Security, naive prompt](docs/screenshots/haiku2-security-naive.png)<br>**Security Center, naive-prompt real model** |
| ![Chaos brownout without retries](docs/screenshots/haiku2-chaos-brownout-off.png)<br>**Chaos, brownout, retries off**: 0/20 | ![Chaos brownout with retries](docs/screenshots/haiku2-chaos-brownout-on.png)<br>**Chaos, brownout, retries on**: 20/20 |
| ![Real Haiku run](docs/screenshots/haiku-run-graph.png)<br>**A real research run** (round 1) | ![Real run cost](docs/screenshots/haiku-run-cost.png)<br>**Cost per step and model** |
| ![Security Center, real model](docs/screenshots/haiku-security.png)<br>**Security Center, real model** (round 1) | ![Arena, real contenders](docs/screenshots/haiku-arena.png)<br>**Agent Arena**, 5 Haiku contenders |
| ![Chaos, brownout without retries](docs/screenshots/haiku-chaos-brownout.png)<br>**Chaos, n=6**: brownout, retries off | ![Chaos, crash loop with 5 attempts](docs/screenshots/haiku-chaos-crash.png)<br>**Chaos, n=6**: crash loop, 5 attempts |
| ![Memory Lab, real reader](docs/screenshots/haiku-memory.png)<br>**Memory Lab** with a real reader | ![Paired experiment](docs/screenshots/haiku-paired-experiment.png)<br>**Paired prompt edit**: not supported, refused |
| ![Real failures](docs/screenshots/haiku-autopilot-failures.png)<br>**Failure Intelligence**, Haiku diagnosis | ![Real experiment](docs/screenshots/haiku-autopilot-experiment.png)<br>**Optimizer candidate** refused |
| ![Real FinOps](docs/screenshots/haiku-finops.png)<br>**FinOps** (round 1): real calls, 0% simulated | ![Real trace](docs/screenshots/haiku-run-trace.png)<br>**Trace** of the real run |

## Visual tour

Every page below is captured from the running stack on the offline mock provider (so costs are simulated and
scores describe scripted agents). Screenshots are regenerated by `make screenshots` and
`node apps/web/tests/capture-v2.mjs`.

### Core platform

| | |
|---|---|
| ![Overview](docs/screenshots/overview.png)<br>**Overview**: runs, cost, failures and worker health | ![Agents](docs/screenshots/agents.png)<br>**Agents**: immutable versions, tool allow-lists |
| ![Workflows](docs/screenshots/workflows.png)<br>**Workflows**: versioned DAGs | ![AI planner](docs/screenshots/workflow-planner.png)<br>**Planner**: AI plans pass the same validator |
| ![Run succeeded](docs/screenshots/run-succeeded.png)<br>**A finished run**: live DAG, timeline, events | ![Approvals](docs/screenshots/approvals.png)<br>**Approvals** bound to exact arguments |
| ![Step panel](docs/screenshots/run-step-panel.png)<br>**Step inspection**: attempts, model calls, tool calls | ![Report artifact](docs/screenshots/artifact-preview.png)<br>**Report artifact** with citations |
| ![Settings](docs/screenshots/settings.png)<br>**Settings** and the offline-mode indicator | ![Run inspector](docs/screenshots/run-inspector.png)<br>**Run Inspector**: nine views of one run |

### Autopilot

| | |
|---|---|
| ![Autopilot overview](docs/screenshots/autopilot-overview.png)<br>**Overview** of the improvement loop | ![Failure intelligence](docs/screenshots/autopilot-failures.png)<br>**Failure intelligence** with evidence ids |
| ![Evaluations](docs/screenshots/autopilot-evaluations.png)<br>**OpenEval** datasets and runs | ![Gates](docs/screenshots/autopilot-gates.png)<br>**Promotion gates** |

### Engineering labs

| | |
|---|---|
| ![Flight index](docs/screenshots/flight-index.png)<br>**Flight Recorder** index | ![Flight playback](docs/screenshots/flight-playback.png)<br>**Playback**: step, scrub, integrity checks |
| ![Flight compare](docs/screenshots/flight-compare.png)<br>**Structured run diff** | ![Chaos Lab](docs/screenshots/chaos-lab.png)<br>**Chaos Lab**: seeded fault plans |
| ![Chaos scorecard](docs/screenshots/chaos-scorecard.png)<br>**Scorecard** with defined metrics | ![Chaos compare](docs/screenshots/chaos-compare.png)<br>**Before/after** under the same fault schedule |

### Operations

| | |
|---|---|
| ![Observability](docs/screenshots/observability.png)<br>**Observability** and telemetry | ![Workers](docs/screenshots/workers.png)<br>**Workers**: health, leases, load |

## Architecture

```mermaid
flowchart LR
    UI[Next.js dashboard] -->|REST + SSE, JWT| API[FastAPI]
    API --> PG[(PostgreSQL<br/>source of truth)]
    W1[Worker 1] & W2[Worker N] -->|SKIP LOCKED claims,<br/>fenced writes| PG
    W1 & W2 --> LLM[Anthropic / OpenAI / mock]
    W1 & W2 --> FS[(artifact storage)]
    PG -. events .-> API
    RD[(Redis: wake-ups only)] -.-> W1 & W2 & API
```

```mermaid
flowchart TB
    subgraph Core[Durable execution]
      E[Engine v2: leases, fencing, branches, fan-out, sub-workflows]
      FR[Flight Recorder]
    end
    subgraph Labs[Engineering labs, offline]
      CH[Chaos Lab] --> E
      SEC[Security Center] --> E
      ARENA[Arena / OpenEval] --> E
      MEM[Memory Lab] --> E
    end
    subgraph Loop[Autopilot]
      FI[Failure Intelligence] --> RE[Rule engine + optimizer] --> EXP[Paired experiments] --> GATE[Gates + admin] --> REL[Release Center: canary, rollback]
    end
    E --> FR --> FI
    FIN[FinOps budgets] -. checked before every model call .-> E
    POL[Offline execution policy] -. guards every provider call .-> E
    REL -. pins versions for new runs .-> E
```

Details, sequence diagrams and lock ordering: [docs/architecture.md](docs/architecture.md) (1.x) and
[AGENTOS_2_ARCHITECTURE.md](AGENTOS_2_ARCHITECTURE.md) (2.0).

## Features

* **Workflows:** versioned, immutable DAGs with `agent_task`, `tool_task`,
  `approval_gate` and `aggregation` nodes. Validation checks cycles (Kahn's
  algorithm), missing or duplicate keys, unauthorized tools, references and limits.
  AI planning works in two forms: synchronous (`/workflows/plan`) and as a durable
  planner step (`/runs/planned`), with bounded replanning on invalid plans.
* **Engine:** leases, heartbeats, fencing, exponential backoff with jitter, retry
  deadlines, transient vs permanent classification, cooperative cancellation, run
  deadlines, budget reservation and reconciliation, per-run concurrency limits.
* **Agents:** a bounded loop (steps, cost, time) with checkpointing after every turn,
  JSON output contracts with one repair turn, and refusal handling.
* **Providers:** Anthropic (official SDK, Messages API, adaptive-thinking models,
  server-side refusal fallback on supported models), OpenAI (official SDK,
  Responses API, `store=false`), and a deterministic mock with scenarios for
  research, analysis, writing, planning, invalid plans, rate limits, timeouts and
  forbidden tools.
* **Tools:** calculator (hand-written parser, no `eval`), PostgreSQL full-text
  document search with citations, CSV/JSON reader (workspace-confined, size-capped,
  optional JSON Schema), and an approval-gated artifact writer (atomic,
  content-addressed, idempotent, fenced). No shell, no HTTP fetch.
* **Observability:** a persisted per-run event log, SSE with `Last-Event-ID` replay,
  OpenTelemetry spans (privacy-aware), structured redacted logs, and metrics
  endpoints for usage, cost, latency, failures, tools and workers.
* **Autopilot:** see [above](#autopilot-evaluation-driven-agent-improvement). Pages:
  Overview, Failures, Improvements, Evaluations, Experiments, Deployments.
* **Dashboard:** overview, agents, workflows (planner + JSON editor + live
  validation + DAG preview), the run detail page (live DAG, step panel with
  attempts, model calls, tool calls, approvals and events, a timeline of attempts,
  artifacts), approvals, artifacts, observability and settings.

## Tech stack

Python 3.13, FastAPI, Pydantic v2, SQLAlchemy 2 (async, asyncpg), Alembic,
PostgreSQL 16, Redis 7, structlog, OpenTelemetry, `anthropic` 2.3, `openai` 3.37,
pytest, ruff, mypy `--strict` · Next.js 16 (App Router, Cache Components),
TypeScript strict, Tailwind 4, Radix primitives, React Flow 12, TanStack Query 5,
Recharts 3, openapi-typescript + openapi-fetch, Playwright · Docker Compose, uv, npm.

## Quick start

Requires Docker. No API keys are needed: everything runs on the mock provider.

```bash
cp .env.example .env            # optional; defaults work for local use
make up                         # postgres, redis, migrate+seed, api, 2 workers, web
open http://localhost:3000      # admin@agentos.dev / agentos-admin
make demo                       # runs and verifies all three demos via the API
make autopilot-demo             # the three Autopilot demos
```

Autopilot's own model calls (diagnosis, optimizer) default to the mock as well. To use a
real model, set `diagnosis_provider`/`optimizer_provider` in the workspace policy
(`PUT /api/v1/autopilot/policy`). Anthropic uses `AGENTOS_AUTOPILOT_ANTHROPIC_MODEL`
(default `claude-haiku-5-5`, low effort). OpenAI needs `AGENTOS_AUTOPILOT_OPENAI_MODEL`.
Spend is capped per optimization cycle.

To use a real model, add `ANTHROPIC_API_KEY` (and optionally
`AGENTOS_PLANNER_PROVIDER=anthropic`, then re-seed with
`AGENTOS_DEMO_PROVIDER=anthropic`). OpenAI needs `OPENAI_API_KEY` **and**
`AGENTOS_OPENAI_MODEL`; no default model ID is shipped. A failing real provider is
reported as an error, never silently replaced by the mock.

Local development without containers for the app processes:

```bash
make install
docker compose up -d postgres redis
make dev                        # api :8000, 2 supervised workers, web :3000
```

## Example workflows

| Demo | Shows | Run |
|---|---|---|
| [Research report](examples/research-report/README.md) | 3 parallel research agents -> analysis with the calculator -> writer -> approval -> Markdown artifact with citations, conflicts and missing evidence | `make demo-research` |
| [Failure recovery](examples/failure-recovery/README.md) | A worker process is killed mid-step; lease expiry; another worker retries; completed steps are not re-run | `make demo-recovery` |
| [Permission enforcement](examples/approval-workflow/README.md) | Reject (no side effect) -> resubmission -> approve -> crash right after the write -> exactly one artifact; unauthorized tool denied | `make demo-permissions` |

Presenter notes: [docs/demo-script.md](docs/demo-script.md).

## API

REST under `/api/v1` with OpenAPI at `http://localhost:8000/docs`. It provides
idempotency keys for run creation and approvals, pagination, and an SSE stream per
run. See [docs/api.md](docs/api.md).

## Failure-recovery design

At-least-once execution of retryable steps, with idempotency for side effects. It
does **not** claim exactly-once. Leases plus fencing tokens prevent concurrent
owners and stale writes. Checkpoints prevent re-running finished model turns.
Idempotency keys plus unique constraints prevent duplicate artifacts. Full
semantics, guarantees and gaps: [docs/failure-semantics.md](docs/failure-semantics.md).

## Security model

Workspace-scoped authorization on every endpoint and three roles. Tool policy,
approval binding, path confinement, request size limits, rate limits, redaction,
and production startup checks are all enforced in code. Prompt-injection resistance
comes from the policy layer, not the prompt. See [docs/security.md](docs/security.md).

## Benchmarks

Apple M1, 8 GB RAM, PostgreSQL 16 in Docker Desktop; real worker processes; numbers
computed from persisted timestamps. Full methodology and tables:
[docs/benchmarks.md](docs/benchmarks.md).

| Measurement | Result |
|---|---|
| Throughput, no-op 20-node fan-out, 40 workflows | 118–222 steps/s (1–4 workers) |
| Dependency-aware parallelism, 8 × 0.5 s nodes | 1.27 s vs 4.65 s sequential: **3.68× speedup** (ideal 8×) |
| Crash recovery, lease TTL 2 / 5 / 10 s | median detection 2.00 / 5.05 / 9.51 s; **18/18 recovered**; 0 duplicate attempts; 0 completed-step re-executions |
| Orchestration overhead per single-call agent step | ~76–87 ms p50 |
| Autopilot cycle (mock agent, 16 validation cases × 3 trials) | fix 43.8% → 100% (CI95 [+0.31, +0.81]); fake "cheaper" candidate refused; live regression 0% → 100% failures, 0% after rollback; [report](docs/autopilot-benchmarks.md) |
| Optimization: batched event writes under the run lock | **+11.6% to +22.1%** median steps/s (A/B, 4 interleaved reps, ranges non-overlapping) |

![throughput](docs/benchmarks/throughput.png)

## Testing

```bash
docker compose up -d postgres redis
make test        # backend tests on real PostgreSQL; 3 opt-in live provider tests skip (no keys)
make lint typecheck web-check
make up && make e2e   # Playwright against the running stack
```

Unit tests cover the DAG, state machines, retry, pricing, tools, redaction,
provider normalization through the real SDKs with a mocked transport, and the
planner. Integration tests cover concurrent claims, fencing, lease expiry,
cancellation, budgets, approvals, event ordering, idempotency, authorization, SSE
replay, Redis outage, and **real worker processes killed mid-step**. Results:
[TEST_RESULTS.md](TEST_RESULTS.md) and [AUTOPILOT_TEST_RESULTS.md](AUTOPILOT_TEST_RESULTS.md)
(Autopilot: OpenEval graders and statistics, optimizer restrictions, versioning triggers,
the full cycle on real workers) and [AGENTOS_2_TEST_RESULTS.md](AGENTOS_2_TEST_RESULTS.md) (every 2.0 phase, with the
bugs that tests and benchmarks found).

### AgentOS 2.0 benchmark highlights

Full tables, methodology and caveats: [AGENTOS_2_BENCHMARKS.md](AGENTOS_2_BENCHMARKS.md). Apple M1, 8 GB; offline.

| Measurement | Result |
|---|---|
| Engine v2 vs previous engine (control, frozen checkouts) | throughput ratio 0.90-1.07; no meaningful slowdown |
| Crash inside a nested workflow, lease TTL 2 / 5 / 10 s | 18/18 recovered, 0 duplicate attempts |
| Chaos: provider-brownout / worker-crash-loop completion, retries off -> on | 0% -> 100% (paired CI excludes 0); infrastructure-hiccups 33% -> 100% |
| Security: gullible-model attack corpus | 28/28 contained, 0/8 false positives, 0 trace leaks (5 repetitions) |
| Memory: stale summaries at 12 updated / 5 expired facts | 17/17 stale answers vs 0/17 for refreshed or retrieval |
| Canary routing, 50k run ids x 8 targets x 4 seeds | max error 0.74 pp; nested sets in 32/32 cells |
| Rollback rules on synthetic arms | false rollback <= 5% for a healthy canary; `insufficient_evidence` at small n |
| Live canary rollback, 5 repetitions | 5/5 automatic, 77/77 failing canary runs, 0/160 mis-pinned runs |

## Engineering tradeoffs

* **PostgreSQL as the queue.** It gives transactional state and leases with no extra
  broker. The cost is a per-run serialization point (the run row lock), which the
  benchmarks measure. A Kafka/NATS design would scale further but would add a second
  source of truth.
* **Per-run lock ordering** over finer-grained locks: simple and deadlock-free by
  construction, at the price of serializing transitions within one run.
* **Polling plus Redis wake-ups**, not `LISTEN/NOTIFY`: works through connection
  poolers and degrades gracefully. Redis is an optimization, never a dependency.
* **SSE over WebSockets**: one-way, proxy-friendly, and resumable with
  `Last-Event-ID`.
* **Hand-written mock policies** instead of recorded responses: deterministic, yet
  they consume real tool output, so the pipeline (search, calculator, citations) is
  genuinely exercised.

## Engineering lessons from building 2.0

* **A benchmark is a test of your rules.** The deployment benchmark showed the first rollback rule rolled back a
  healthy canary 28-37% of the time at the SLO boundary. Adding one-sided Fisher and binomial tests fixed it.
* **Invisible work cannot be optimized.** Identical tool calls are replayed from the record, so redundant calls left no
  rows and the cost analysis could not see them. A per-attempt replay counter made them measurable.
* **Observability gaps hide in the joins.** Run ids were never linked to a trace id and worker spans started new
  traces until a root span and a stored `traceparent` were added.
* **Audit rows need their own transaction** (a refusal that rolls back with the failing request leaves no trail), and
  writing them while holding a parent-row lock needs `FOR NO KEY UPDATE` to avoid self-deadlock.
* **Enforce policy where it cannot be skipped.** Offline mode is checked inside the adapter and again under the HTTP
  transport; a documented flag would not have caught a direct client call.

## Known limitations

Highlights (full lists: [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md),
[AGENTOS_2_KNOWN_LIMITATIONS.md](AGENTOS_2_KNOWN_LIMITATIONS.md)):

* **Real-model evidence is minimal.** Apart from the small real-model runs above (about 7,500 calls), every score, including the
  benchmark tables, is from scripted offline providers. Nothing here is evidence that a real
  model is robust, accurate or injection-resistant, and OpenAI was never run.
* **"Production" in the Release Center is a production-style local simulation**, not a hosted service. Do not read
  this repository as production-ready; it has not been load-tested or externally reviewed.
* The Arena corpus has no dedicated tool-use-conflict family (calculation and grounded QA are used); the leaderboard is
  not sortable.
* Optimization proposes 9 rule types; tool-routing, memory-policy and topology candidates are not implemented.
  FinOps has no workspace-wide concurrency limits.
* Demo B and E timing varies with the clock and run ids; two engine tests (`test_chaos` dropped-connection retry,
  `test_engine_budget` reservation release) have failed once each under heavy load in full runs and pass in isolation.


See [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md). Highlights:

* The Anthropic adapter was exercised against the live API only briefly (one smoke test and the Haiku runs above); it
  was otherwise tested with a mocked HTTP transport. The OpenAI adapter was **never run live**. Opt-in smoke tests
  exist (`AGENTOS_LIVE_TESTS=1`).
* Development authentication only (seeded users, HS256, `localStorage` tokens).
* Single PostgreSQL instance. No horizontal API autoscaling story, no multi-region
  setup.
* No HTTP fetch tool. Document search is keyword-based (no embeddings).
* The CI workflow is included but has not been run on GitHub from this environment.
* Autopilot's diagnosis and optimizer ran against Claude Haiku once (small sample, no improvement found); its
  benchmarks use a scripted agent. See [AUTOPILOT_KNOWN_LIMITATIONS.md](AUTOPILOT_KNOWN_LIMITATIONS.md).

## Roadmap

Real-model evaluation of Arena, Autopilot and Memory Lab under spend caps (see the enabling guide) · OIDC authentication and per-user rate limits · an SSRF-safe HTTP fetch tool ·
embedding search (pgvector) · a drag-and-drop DAG editor · per-tool approval
policies (auto-approve rules, deny lists) · `LISTEN/NOTIFY` wake-ups · partitioned
event tables and retention · a Kubernetes manifest with worker autoscaling on queue
depth.

## Author

Built by **Dev Mulchandani** ([@DMgaming00](https://github.com/DMgaming00)).
Issues and pull requests: <https://github.com/DMgaming00/AgentOS>.

## License

MIT, see [LICENSE](LICENSE). Copyright (c) 2026 Dev Mulchandani.
