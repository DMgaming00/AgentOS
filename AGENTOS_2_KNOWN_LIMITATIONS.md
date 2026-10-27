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
