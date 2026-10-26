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
