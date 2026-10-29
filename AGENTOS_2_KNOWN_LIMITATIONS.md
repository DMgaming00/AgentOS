# AgentOS 2.0: known limitations

Updated each phase. Items are things that are not done, not verified, or weaker than they may look.

## Verification gaps (carried over)

* No real Anthropic or OpenAI request has ever been made; the real adapters are verified only through the
  official SDKs with an in-memory transport. The opt-in live tests have never been run.
* CI has not run on GitHub; its steps were run locally.
* Repository has no git history prior to the 2.0 upgrade.

## Phase 1 (offline guarantee)

* The guard covers AgentOS's own model-request paths. It is not an OS-level network sandbox; code outside
  those paths could open a socket.
* The guarded transport replaces the SDK default transport, so proxy environment variables are not honored
  by real clients (documented in `docs/ENABLE_REAL_CLAUDE.md`).
* The spend cap is an application-level ledger check before each request, not a provider-side limit, and an
  in-flight request is counted only after it returns. Concurrent workers can overshoot by up to
  (workers × reservation) in a race.
* The fixture provider matches by digest or substring; it does not simulate streaming or provider quirks.
* The hang in `test_plan_endpoint_with_mock` is attributed to overlapping pytest sessions (probable, not
  deliberately reproduced); see `docs/AGENTOS_2_AUDIT.md` §2.

## Phase 2 (engine v2)

* **Nested workflows use the latest version** of the named workflow at the moment the child starts; the version is
  recorded on the step, but a mid-run publish of a new version changes what later parents start. Static detection of
  workflow cycles (A → B → A) is not done; the depth limit (default 3, max 5) is the guard.
* **Child cost accounting** is rolled up to the parent only when the child finishes; the parent's budget is not
  decremented while the child runs (the child's cap is the parent's remaining budget at start).
* **`fan_out` children run the same template**; there is no per-item branching, no nested fan-out, and `max_items`
  is capped at 20. Child results are returned in input order; a single failing child fails the node and the run
  (no partial-result mode).
* **A condition on a missing value is false**, so a typo in a `when.ref` silently skips a branch. Definition
  validation checks the reference root (`input.` or a declared dependency) but not that the path exists.
* **Pause does not stop running steps** and the run deadline keeps ticking while paused.
* **Priority aging** is evaluated per run from the oldest ready step; there is no per-workspace fairness or quota.
* **Delivery semantics remain at-least-once.** A crash between an external effect and the row that records it can
  repeat the effect for a tool that cannot be made idempotent (see architecture §2.2).
* **Benchmarks** measure the scheduler and database with aggregation and calculator nodes (no model calls) on one
  8 GB laptop with PostgreSQL in Docker; results move by more than a factor of 3 with ambient load, so the report gives
  medians and ranges over repetitions, not single numbers.

## Phase 3 (Flight Recorder)

* **Only runs executed after migration `0005` have recorded model calls.** Older runs replay as event timelines
  (statuses, tool executions, approvals) but show no model requests/responses, and agent runs without recordings
  cannot be re-executed.
* **Re-execution isolation is side-effect isolation, not a separate workspace** (see architecture §3). Read-only
  tools (`document_search`, `structured_data_reader`) do run for real, against the live workspace documents; if
  those changed since the original run the re-execution will flag divergence rather than fail.
* **Divergence is detected by request digest.** A re-execution that behaves differently but sends an identical request
  cannot be distinguished; one that sends a different request still receives the recorded response.
* **Captured prompts are sanitized, not anonymized**: free-text personal data in a prompt that does not match a secret
  pattern is stored (as it is in run inputs and outputs today). Treat recordings like run data.
* **Truncation**: requests over 200 KB keep the first and last 8 messages; individual text fields are cut at 20,000
  characters. Truncated records say so.
* **Spans**: `trace_id`/`span_id` are the OpenTelemetry ids active when the event was written; they correlate with a
  collector only if one is configured (OTLP export was never exercised against a collector).

## Phase 4 (Chaos Lab)

* **Fault injection is not a network or OS sandbox**: faults are applied inside the platform's own code paths. A
  `postgres_interruption` kills only the injecting process's connections, but in a shared process (in-process test
  workers) that includes every concurrent step.
* **A worker crash takes down every step that worker is running**, so with high crash probability a step can lose its
  worker several times in a row and exhaust its retries; the benchmark shows this (see AGENTOS_2_BENCHMARKS.md).
* **Approvals in experiments are decided by a simulated reviewer**; results say nothing about human review latency.
* **The same fault schedule is guaranteed only where two configurations reach the same coordinates**
  (seed, case, step, attempt, call): a configuration that makes different calls faces different faults.
* **Detection time is "first platform reaction recorded after the injection"**, which can be an unrelated reaction in the
  same run; silent absorptions are counted separately rather than hidden.
* Chaos experiments use the mock provider offline; they measure the platform's behavior, not any model's.

## Phase 5 (Security Center)

* The injection model is a **mock that obeys every directive by design**. Passing means the *platform's enforcement*
  held; it is not evidence about any real model.
* The corpus is **28 hand-written attacks and 8 benign controls** across 7 goals and 4 surfaces. New attack classes need new
  cases (and a new suite version). Directive syntax is uniform (`[[CALL_TOOL ...]]`), so natural-language obfuscation
  of instructions is not exercised.
* Secret scanning covers **persisted trace data** only; structured logs of other worker processes are not scanned.
* The suite needs workers (it runs real workflows), takes about 25–30 s, and temporarily writes fixture documents into the
  workspace and creates a throwaway workspace; both are removed at the end, but a crash between phases can leave them
  until the next run's cleanup or a manual removal.
* Security-run agents appear in the Agents list (`Security Test Agent`, labeled as a fixture).

## Phase 6 (Agent Arena)

* Contenders are **scripted policies**, not models. Scores show the effect of configuration and the correctness of the harness; they say nothing about real-model quality. The best contender saturates the corpus (100%), so the corpus cannot rank near-perfect agents.
* The corpus draws on **25 facts from 3 fictional documents**; facts recur across splits in different combinations. Splits protect against tuning on the cases used for selection, not against fact familiarity.
* 111 cases with 7–20 per family-split give wide intervals; many adjacent contenders legitimately tie.
* The test split can be scored once **per arena run**; nothing stops a user from creating a new arena run and scoring it again, so repeated peeking across runs is possible (each run records its own dataset hashes). Optimizers (Autopilot) never see validation or test answers.
* Latency and cost columns come from the simulated mock provider.

## Phase 7 (Memory Lab)

* **No embeddings.** Retrieval is keyword full-text; paraphrased queries will miss records a semantic index would find. An optional vector index was not built.
* The experiment reader is a **scripted policy** with fixed matching rules (longest entity name contained in the question, conversation before memory). Results show how a strategy's context affects that reader, not how a real model uses memory; with another matching rule the ranking of strategies could change.
* 38 questions over 26 stored facts from one seeded scenario; retrieval and "persistent" tie on accuracy there, and the context-size difference is the real finding.
* The sensitive-content check is a pattern heuristic (known key shapes, `secret: value` pairs). It will miss other secrets and personal data.
* Delete is soft: content stays in `memory_versions` for audit. There is no admin purge, so a user who needs content erased must be handled out of band.
* Experiments create `mlab-…` namespaces, `memory-lab-…` datasets and the Memory Lab Agent in the workspace; they are not cleaned up automatically (they are the experiment's evidence).
* Expiry is enforced on read immediately and recorded by the coordinator tick (every worker runs it); the experiment fast-forwards expiry for specific records instead of waiting.

## Phase 8 (Optimization Lab 2.0)

* Not implemented: **tool routing, memory policies and workflow-topology proposals** from the spec's candidate list. Topology changes would need workflow versioning and DAG/permission validation of proposed definitions.
* Rules read **error text and counts** from traces; they are only as good as those signals. `numeric_as_string` and `missing_required_fields` match `jsonschema` messages and will not fire for other wording.
* Thresholds (e.g. "≥2 failed steps") are fixed per rule version; they are conservative defaults, not tuned.
* `cheaper_model` uses list prices from the price table (simulated for mock models) and is explicitly a projection; it has no measured evidence until an experiment runs.
* The latency comparison is reported but is **not a deployment gate**: offline latencies are wall-clock times of a mock provider on a shared machine and too noisy to gate on.
* The model-proposed and rule-proposed optimizers can produce the same configuration (same content hash, one version, two candidate records).

## Phase 9 (FinOps)

* **All amounts are estimates**; offline they are simulated and say so. Real invoices can differ (caching, batching, billing rounding).
* Budget spend uses recorded `model_usage` cost; **calls with unknown cost contribute nothing** and are only counted. Autopilot diagnosis/optimizer calls count toward spend but are not stopped by these budgets (they keep their own per-cycle cap).
* A budget can be exceeded by a call's *actual* cost over its reserved estimate (the reservation is an estimate); the next call is then denied.
* **Workspace-wide concurrency limits are not implemented**; concurrency is limited per run (`max_concurrency`) and by worker count.
* Anomaly thresholds are fixed defaults, not tuned or learned, and are evaluated per process every 5 minutes; a slowly rising cost can pass under the ratio thresholds.
* "Cost per successful task" is cost per successful *step* (agent) or per succeeded *run* (workflow); a task spanning several steps is not tracked as one unit.
* The cheaper-model suggestion only considers models the workspace policy already approves, and quality is explicitly unverified until an experiment runs.

## Phase 10 (Release Center)

* "Production" is a **local simulation** inside this deployment; there is no hosted environment, traffic shaping, or load balancer. Routing happens when a run is created.
* **No automatic advance**: moving to the next canary percentage needs an administrator, by design. Time-based progression would need minimum soak times and a policy for unattended promotion.
* Canary evidence is **per agent version** measured on finished live steps (`purpose=workflow`, the plan's environment). Workflows that call several agents mix their steps into each agent's own arms; there is no per-workflow canary.
* The security gate reflects the workspace-wide enforcement-layer evaluation, not the candidate specifically.
* The seeded bucket is a hash, so small samples can deviate noticeably from the target percentage (the 5% stage may see no canary traffic for a while); the stage gates require a minimum number of canary steps for that reason.
* Environment pins exist for development and staging only; they are routing pins, not separate databases or deployments.
* The older `monitoring_tick` failure-rate check (policy-level, off by default for auto-rollback) still runs alongside release rules; a release's rules are the configurable path.

## Phase 11 (UX and observability)

* Spans are exported only if OTLP export is enabled; the Trace tab is built from stored records, not from an OpenTelemetry backend. Runs created before migration 0014 have no `traceparent` (their trace tab shows "none recorded").
* The HTTP request span exists only when FastAPI instrumentation is installed and enabled; `run.create` still starts a trace without it.
* Database latency is a probe from the API process, not query-level workload telemetry (no `pg_stat_statements`).
* Worker "utilization" is in-flight steps over configured concurrency at the last heartbeat (a 3-second-resolution snapshot, not an average).
* The Workers page shows heartbeat-based health only: there is no remote control (drain, restart) from the UI.
* Telemetry reads at most 50,000 rows per metric family per request; very busy workspaces should use shorter windows.
* Keyboard navigation relies on native elements and Radix primitives; a full screen-reader audit was not performed.

## Added at completion (Phases 12-13)

* **Intermittent chaos test.** `test_infrastructure_faults_do_not_corrupt_state` (Redis interruption plus delayed notifications at probability 1.0) failed in 2 of ~20 observed runs with 3/4 trials completing; the cause is not identified (the failing assertion now prints the experiment results for the next occurrence). Treat a failure as a possible real recovery gap, not noise. Two other engine tests (`test_a_dropped_postgres_connection_is_retried_not_fatal`, `test_engine_budget` reservation release) have failed once each under load.
* **Arena** has no dedicated tool-use-conflict family (calculation and grounded QA stand in) and its leaderboard is not sortable. Arena scores are scripted offline contenders and carry no claim about real models.
* **Metrics queries degrade on large databases** (about 7,000 runs made the UI e2e suite time out). No pagination or rollup tables for telemetry yet.
* **Rollback rules** use alpha 0.05 one-sided tests; the stated false-rollback rates come from seeded synthetic arms, not real workloads.
* **Demo timing varies**: demos B and E depend on fault timing, the clock and run ids; A, C and D are deterministic on the mock provider.
* The platform has not been load-tested, security-reviewed by a third party or run against any real provider; it is not production-ready.

## Real-model modes (added 2026-10-29)

* The Security Center, Memory Lab and Arena gained opt-in real-model modes, and the Chaos Lab was run against real calls. Only
  Claude Haiku 5.5 was used, once, at small n (see the README's Real-model results); no other model or provider was run.
* In real-model security runs the model sees only its advertised tools, so attacks that need an unlisted tool cannot be
  expressed. Haiku ignored all 28 injections, so the platform's containment of a *followed* real-model attack was not
  exercised; that evidence remains the scripted gullible model's.
* Real-model Arena and Memory Lab results are single runs (one trial per case) and nondeterministic; ties and gaps inside the
  intervals are not evidence.
* Real-model chaos approvals are decided by a simulated reviewer, and the crash-loop result depends on the configured retry
  budget (3 attempts: 1/6; 5 attempts: 5/6).
* Cost figures for real calls are price-table estimates, not invoices; the app's spend cap is an application-level guard.

## Real-model round 2 (2026-10-29)

* The `hard-ops` set is small (150 cases) and Haiku with a calculator scores about 93% on it unconstrained, so the repair
  results are for a deliberately constrained baseline (a 3-step budget), not evidence that Autopilot improves a strong agent.
* The held-out test result (47.5% -> 89.0%, paired CI [+0.29, +0.54]) was still marked not eligible by the strict per-case
  regression gate (2 regressed cases); nothing was promoted, and the gate was not changed to make it pass.
* Real-model security: with a naive prompt Haiku followed only 4 of 112 injections across 4 runs, so containment of a followed
  real-model attack rests on 4 observations. Two grading rules were wrong for real models and were fixed (own tool mistakes are
  not false positives; a flood the model did not carry out is not a failure); the earlier run's verdicts were produced by the old rules.
* Chaos at n=20: the crash-loop arms were cancelled by the 10-minute experiment cap (0/20 both), a capacity and lease-timing limit
  of 20 runs on 2 workers, not a retry-budget measurement. A first n=20 run silently used scripted agents (the spend ledger did
  not move) and was discarded.
* An optimizer candidate that switched the agent to Sonnet was evaluated by default and cost about $5; candidates that change the
  model should be excluded from unattended real-model runs.
* Costs are price-table estimates, and the FinOps total mixes simulated and real calls in a database used for demos.
