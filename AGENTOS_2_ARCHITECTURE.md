# AgentOS 2.0: architecture

This document grows phase by phase and describes only what exists in the repository. The 1.x system
(durable engine, agent runtime, Autopilot) is described in `docs/architecture.md` and
`AUTOPILOT_ARCHITECTURE.md`; this file covers 2.0 additions. Audit and plan: `docs/AGENTOS_2_AUDIT.md`.

## 1. Offline execution boundary (Phase 1)

Every model request in the system ends in a provider adapter's `generate()`. Paid providers are blocked
in code unless the execution policy allows them; no configuration document is relied upon.

```mermaid
flowchart LR
  subgraph callers[Callers]
    W[Worker model_calls] --> F
    P[Planner] --> F
    A[Autopilot diagnosis / optimizer] --> F
  end
  F[get_provider] --> M[MockProvider<br/>no network]
  F --> X[FixtureProvider<br/>stored, versioned, simulated]
  F --> R[Anthropic / OpenAI adapter]
  R --> G1{{check_provider_call<br/>layer 1: start of generate}}
  G1 -->|blocked| E[OfflineModeError<br/>provider_blocked_offline]
  G1 -->|allowed| S{{enforce_real_spend<br/>cap vs model_usage ledger}}
  S -->|over cap| B[BudgetExceededError]
  S -->|ok| T{{GuardedTransport<br/>layer 2: every HTTP request}}
  T -->|blocked| E
  T -->|allowed| N[(network)]
```

* **Policy** (`ExecutionPolicy`): live requires `AGENTOS_EXECUTION_MODE=live`,
  `AGENTOS_ALLOW_PAID_PROVIDERS=true`, `AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS=true` and
  `AGENTOS_MAX_REAL_API_SPEND_USD>0`. The error lists exactly which conditions fail.
* **Two independent layers.** (1) The adapter checks before building a request. (2) The real SDK clients are
  created with `GuardedTransport`, which re-checks per HTTP request, so `provider.client.messages.create()`
  called directly cannot connect either.
* **In-memory contract transports** (`httpx2.MockTransport`) are recognized and allowed offline, because they
  cannot reach the network; this keeps the SDK request/response contract tests runnable without credentials.
* **No silent fallback.** A refused or failed real-provider call fails the attempt with a typed error
  (`provider_blocked_offline` is permanent: no retry) and is recorded in `model_usage` under the *real*
  provider's name with status `failed`. The mock is never substituted.
* **Spend ledger.** `real_spend_usd()` sums recorded estimated cost for `anthropic`/`openai`;
  `enforce_real_spend()` runs inside the worker's model-call path and Autopilot's LLM wrapper, inside the
  `try` that records failed calls, so a refusal releases its budget reservation like any provider failure.
* **Fixture provider** (`providers/fixtures.py`, files in `fixtures/model_responses/*.v<N>.json`): replays
  stored responses selected by exact request digest, by substring rules, or by explicit `fixture_id`. Sets must
  declare `simulated: true`; a missing fixture raises `fixture_missing`; responses are priced with a simulated
  price (`fixture/*`) and labeled `fixture:<set>@v<N>`. `fixture_entry()` records a sanitized (redacted)
  response keyed by digest for use when real calls are enabled later.
* **Visibility.** `/api/v1/system` returns `execution` (mode, blockers, label); the header banner always shows
  `Offline: paid providers blocked` or `LIVE: paid providers enabled`, and marks keys present as blocked.
* **Migration `0003`** widens the agent provider check constraint to allow `fixture`.

What this does *not* guarantee: it controls AgentOS's model-request paths. It is not a network sandbox for
arbitrary code, and a modified build could remove the guard.

## 2. Execution engine v2 (Phase 2)

The durable engine (PostgreSQL state, `SKIP LOCKED` claims, lease tokens, fenced writes, heartbeats, retry
scheduling, immutable attempt history, idempotency keys) is unchanged. Phase 2 adds constructs on top of it,
none of which weakens those guarantees. Migration `0004`.

### 2.1 New workflow constructs

| Construct | Definition | Engine behavior |
|---|---|---|
| **Conditional branch** | any node: `when: {ref, op, value}` | Evaluated by the engine when all dependencies are terminal, against `input.*` or a declared dependency's output. False → the step is **skipped** (`step.skipped`, reason `condition_false`). An unresolvable ref or a type error is false, never an exception. 11 operators (`eq ne gt gte lt lte in not_in contains exists truthy`) |
| **Skip propagation / joins** | `trigger: all_succeeded` (default) or `any_succeeded` | A step with a skipped dependency is skipped (`upstream_skipped`) unless `any_succeeded` and at least one dependency succeeded. Skipped steps are never claimed and have no output. A step whose dependents were all skipped is reported as a run output |
| **Bounded fan-out / fan-in** | `type: fan_out`, `over` (ref to a list), `template` (an `agent_task` or `tool_task`), `max_items` ≤ 20, `on_overflow: fail\|truncate` | Never claimed by a worker. When ready, the engine creates `key[0..n-1]` child steps (each depends on the fan_out node's own dependencies, so they run in parallel) and moves the node to `waiting_child`. When every child has succeeded the node succeeds with `{"results": [...in input order], "count": n}`, so downstream nodes depend on it as a normal fan-in. More than `max_items` fails the node (default) or truncates (explicit). Expansion beyond `max_dag_nodes` fails. List items are data: they are exposed as `item`/`index` and never template-resolved |
| **Nested workflow** | `type: sub_workflow`, `workflow` (name), `input` | The engine starts the *latest version* of that workflow as a **child run** (`parent_run_id`, `parent_step_id`, `depth`) and moves the step to `waiting_child`. The child inherits the parent's priority, a deadline no later than the parent's, and a cost cap equal to the parent's remaining budget. When the child finishes, the coordinator's `subflow_tick` completes the parent step with `{"child_run_id", "output"}` (or fails it with `reason: child_failed`) and adds the child's spend to the parent. Depth limit `AGENTOS_MAX_NESTED_DEPTH` (default 3) |
| **Priority** | run `priority` 0–9 (`POST /workflows/{id}/runs {"priority": n}`) | Runs are claimed in order of *effective priority* = priority + (run age ÷ `AGENTOS_PRIORITY_AGING_SECONDS`, default 30). A priority-0 run that is 9 intervals older than a newly created priority-9 run outranks it, so low-priority work cannot be starved by later arrivals. (Runs created together age together and keep their gap.) Age is measured from run creation: a first version aged the *next ready step*, which restarts every time a multi-step run advances, and a benchmark showed it starving low-priority runs once per step |
| **Pause / resume** | `POST /runs/{id}/pause`, `/resume` (developer+) | A paused run (and its child runs) claims no new step; running steps finish; state, retries and approvals are untouched; the run deadline keeps ticking. Idempotent |
| **Cancellation** | existing | Now also cancels `waiting_child` steps and, parent→child, the child runs |

`waiting_child` is a step state without a lease: it is durable, visible in the DB and UI, and recovered by the
coordinator, not by a process. State machine additions: `PENDING → {FAILED, SUCCEEDED, WAITING_CHILD}` (engine-internal)
and `WAITING_CHILD → {SUCCEEDED, FAILED, CANCELLED}`.

**Lock order** is unchanged and deadlock-free by construction: parent run row before child run row; a child's own
transactions never lock the parent. Child→parent propagation is therefore a separate coordinator transaction
(`subflow_tick`, skip-locked, idempotent), so a crash between "child finished" and "parent updated" only delays the
parent by one tick.

### 2.2 Delivery semantics (precise)

* **Steps run at least once** while retryable: a step whose worker dies (lease expiry) is run again; the
  attempt history records `lease_expired` + the new attempt. It is **not** exactly-once for arbitrary external
  effects, and AgentOS does not claim that.
* **A completed step is never run again.** Recovery only touches `running` steps whose lease expired.
* **Stale writers are fenced.** Every completion/failure/checkpoint checks the current lease token; a worker that
  lost its lease cannot overwrite the new result (the late result is recorded as `stale` and discarded).
* **Side effects.** Every tool call carries an idempotency key (run/step scope + tool name + canonical argument
  hash), and the database allows at most one *successful* execution per key. A retried step that repeats a call
  with a recorded success gets the stored result back (marked `idempotent_replay`) instead of executing again;
  the artifact writer is additionally content-addressed and approval-gated. The remaining window is a crash
  *between* an external effect and the row that records it: a tool that cannot make its effect idempotent could
  repeat there, which is why at-least-once is the stated guarantee.
* **Engine-handled nodes** (`fan_out`, `sub_workflow`) have no worker and no lease; their transitions happen inside
  the transaction that holds the run lock, or in `subflow_tick`, so there is nothing to "execute twice".
* **Approvals** are rows; a pending approval survives any process death and is bound to its argument hash.
* The system converges: every non-terminal state has a recovery path (lease expiry, retry promotion, approval
  expiry, deadline enforcement, child propagation), so each run reaches a terminal state or is bounded by its deadline.

### 2.3 Verification

Unit tests for conditions and definition validation (`test_dag_v2.py`); integration tests for every construct on
real PostgreSQL with in-process workers (`test_engine_v2.py`); fault-injection tests with **real worker
processes** killed mid-step (`test_engine_v2_recovery.py`): a crash inside a child run, a crash after fan-out
children completed (children not re-run), SIGKILL while four fan-out children were running (each recovered once,
no duplicate children), and a pending approval surviving a killed worker. Results: `AGENTOS_2_TEST_RESULTS.md`.
Measurements: `AGENTOS_2_BENCHMARKS.md`.

## 3. Agent Flight Recorder (Phase 3)

Migration `0005`. The Flight Recorder lets a developer see exactly what an agent run did, step through it, compare it
with another run, re-execute it deterministically, and attach a sanitized trace to a bug report.

```mermaid
flowchart LR
  W[Worker: call_model] -->|one transaction| T[(model_usage + events +<br/>model_call_records)]
  T --> TL[load_recording<br/>frames + status deltas]
  TL --> UI[Playback UI<br/>play · step · scrub · jump]
  TL --> DF[diff_recordings]
  TL --> EX[build_export<br/>redact · scrub · fail-closed]
  TL -->|start_reexecution| RR[new run purpose=replay]
  RR --> RP[RecordedProvider<br/>responses from the recording]
```

**What is captured.** For every model call, in the same transaction as its usage row and its event, an immutable
`model_call_records` row: sanitized request (system prompt, messages, tool names and descriptions, model
configuration, agent version id, prompt digest), response (text, tool calls with arguments, finish reason, usage),
latency, error, and a `request_digest` computed over the *unsanitized* request so divergence is detected exactly.
Secrets are redacted before storage; provider-raw payloads (thinking blocks, signatures) are never stored; oversize
requests keep the first and last messages and set `truncated`. A database trigger forbids UPDATE and DELETE.
Events additionally carry `trace_id` and now `span_id`, and each `model.call.*` event carries its `record_id`.

**Recorded replay** (`GET /runs/{id}/flight`) is a pure read over stored rows. A recording is a list of *frames*, one
per event in sequence order, each with the step-status changes it caused (`status_delta`), a severity
(`info|warning|error`) and trace/span ids, plus step attempts, model calls, tool executions and approvals. An
`integrity` block reports event count, first/last sequence, contiguity, gaps and a SHA-256 over the frames. The UI
applies deltas cumulatively, so scrubbing to any frame shows exactly the recorded state; tests assert that the state
after the last frame equals the final step statuses and that the state before the first frame is all `pending`.

**Deterministic re-execution** (`POST /runs/{id}/reexecute`, developer+) creates a **new** run, never touching the
recording. The run has `purpose='replay'` and `replay_of_run_id`, the same workflow version and input, and every agent
step pinned to the exact agent version the original used. In the worker:

| Aspect | Re-execution behavior |
|---|---|
| Model calls | `RecordedProvider` serves the i-th successful recorded response of the same node; no provider, no network, no cost. More calls than recorded → `replay_diverged` (permanent failure). A call whose request digest differs from the recording is still answered but flagged `request_matches_recording: false` in its event |
| Tools | only `calculator`, `document_search`, `structured_data_reader`. For agents the tool list shrinks and any other call is denied by the normal tool policy (`tool.denied`); a `tool_task` for any other tool fails with a policy violation |
| Approvals | `approval_gate` nodes complete immediately as `not_requested_in_replay`; nothing gated is ever executed or requested |
| Visibility | replay runs are tagged and filterable (`?purpose=replay`); the UI labels them and links to the original |

"Isolated test workspace" is implemented as *side-effect isolation* (purpose tag + the rules above), not as a separate
workspace, because agents and workflow definitions are workspace-scoped and copying them would change what is being
tested. This is a documented deviation from the original wording.

**Run comparison** (`GET /flight/compare?a=&b=`) aligns steps by node key and reports, per step: status, branch
(skipped in one run only), retries (failed attempts, not approval resumes), output differences as JSON paths, model
calls (request same? response same?, text snippets), and tool calls as `(tool, argument hash, status)` so a refused call
differs from an executed one; plus run-level status, output, duration, estimated cost, call counts and, for
evaluation-case runs, the pass/fail verdicts (never expected answers).

**Export** (`GET /runs/{id}/export`, developer+) builds a package of the recording and the model call bodies, removes
credential-like keys and lease tokens, redacts secret patterns, then **re-scans the result and refuses to emit it** if
anything credential-like remains (`409 export_refused`). The package carries SHA-256 hashes of the frames and itself.

**Determinism finding.** The first re-execution revealed that the task prompt for a step with several upstream
dependencies depended on database row order (`dependency_outputs` came from an unordered query), so the *same* workflow
could send a differently ordered prompt on each run. Dependencies are now assembled in declared order.

## 4. Agent Chaos Lab (Phase 4)

Migration `0006`. The Chaos Lab tests reliability, not error-message rendering: each fault is injected at the boundary where
production code meets the outside world, so the platform's own retry, policy, fencing, budget and recovery logic is
what answers it.

```mermaid
flowchart LR
  P[ChaosPlan<br/>seed · faults · trials · caps] --> X[create_experiment<br/>seeded runs purpose=chaos]
  X --> R[(workflow_runs<br/>chaos_experiment_id, case_index)]
  R --> W[Worker step]
  W -->|ClaimedStep.chaos| C[ChaosContext.fire<br/>pure hash decision]
  C -->|claim_injection<br/>advisory-locked ledger| L[(chaos_injections<br/>append-only)]
  C --> F1[provider adapter boundary<br/>timeout · 429 · overflow · malformed]
  C --> F2[tool boundary<br/>bad args · timeout · failure · hostile result · duplicate]
  C --> F3[process boundary<br/>worker crash · Redis · PostgreSQL · notifications]
  T[chaos_tick] --> V[simulated reviewer<br/>delay · reject]
  T --> S[safety budget · window · finalize]
  S --> K[compute_scorecard<br/>sealed in chaos_experiments.results]
```

**Faults (17) and where they are injected**

| Boundary | Faults | How the platform sees it |
|---|---|---|
| Provider call (`call_model`, inside the failure-recording `try`) | `provider_timeout`, `provider_rate_limit`, `context_overflow` | The real typed provider errors (`ProviderTimeoutError`, `ProviderRateLimitError`, `ProviderRequestError`), so classification, retry/backoff, budget settlement and the failed-call record all run for real |
| Model response | `malformed_model_response`, `corrupted_fixture_response` | The response text is truncated / replaced and tool arguments mangled after the call; the output contract and the repair turn must catch it |
| Tool execution (`tools/runner`) | `invalid_tool_arguments` (before validation), `tool_timeout` (outlives `wait_for`), `tool_failure`, `malicious_tool_result` (injected instruction in the result), `duplicate_delivery` (the call is delivered again; the repeat has faults off) | Real schema validation, real timeout path, real policy denial, real idempotency replay |
| Step completion | `duplicate_delivery` (`after_complete`) | `complete_step` is called a second time with the same lease token; the fence must reject it |
| Worker process | `worker_crash` (`os._exit(137)` at step start / before a model call / after a tool) | Lease expiry and resumption from the checkpoint (needs real worker processes) |
| Redis | `redis_interruption` (all connections dropped, new ones refused for a window), `delayed_notifications` (publishes delayed randomly, so out of order) | Wake-ups degrade to polling; PostgreSQL stays authoritative |
| PostgreSQL | `postgres_interruption` (terminates *this process's* other connections, identified by `application_name=agentos-<pid>`, so other processes are untouched) | Open transactions fail; the pool reconnects |
| Run budget | `budget_exhaustion` (budget cut to $0 at run creation) | `BudgetExceeded` stops the run before spending |
| Approvals | `approval_delay`, `approval_rejection` (by the simulated reviewer in `chaos_tick`) | Durable wait; a rejected action never executes |

**Reproducibility.** A decision is `sha256(seed | case | step | fault | point | attempt | call key)` mapped to [0, 1) and compared with
the fault's probability: a pure function of coordinates that do not depend on run ids or timing. Which runs may be affected is fixed
up front from the seed alone (`ChaosPlan.affected_cases()`: the `max_affected_runs` lowest hash ranks), and the per-fault cap is
**per step** (`max_per_step`), because a per-run cap made concurrent steps race for the budget and the schedule depend on timing
(a test caught this). The same plan therefore produces the same schedule in every experiment, which is what makes a before/after
comparison "under the same fault schedule" meaningful.

**Safety (enforced in code).** Plans are bounded (≤ 40 runs, ≤ 600 s window, ≤ 8 faults, per-step caps, safety budget ≤ $25);
at most two experiments run per workspace; faults only fire for runs of a *running* experiment and only inside its window
(`ChaosContext` returns no fault otherwise); `AGENTOS_ALLOW_FAULT_INJECTION` disables the whole feature; experiments are cancelled
when estimated spend exceeds the safety budget; runs still unfinished when the window closes are cancelled. Injections are
recorded **before** they are applied (so a crash cannot lose the evidence), the table is append-only (the response `outcome` may be
set exactly once), and a finished experiment is sealed by a trigger. Offline mode means no paid provider is touched.

**Resilience scorecard.** Computed from persisted records only, with the metric definitions and denominators stored in the
result and shown in the UI. There is **no combined score**: the metrics are on different scales and any weighting would be arbitrary.

| Metric | Denominator | Kind |
|---|---|---|
| task completion rate | planned runs | outcome |
| fault exposure | planned runs | descriptive |
| recovery rate | affected runs | outcome |
| retry amplification (attempts / steps; extra attempts per injection) | steps | descriptive |
| time to detection (injection → first platform reaction) | injections with a reaction (silent ones counted separately) | latency |
| time to recovery (injection → step completion) | injections on steps that later succeeded | latency |
| duplicate side-effect prevention (+ duplicate successful executions found, must be 0) | duplicates injected with evidence | safety |
| state consistency (5 invariants per run) | invariant checks | safety |
| budget compliance | runs | safety |
| policy held under hostile output | runs with a malicious tool result | safety |

**Before / after.** `POST /chaos/compare` takes two completed experiments with the same plan hash (same seed, faults and
caps), pairs their per-case outcomes and runs OpenEval's paired bootstrap and exact McNemar test. The configuration under test
is an agent version (`version_overrides`, or an Autopilot `candidate_id`), so a candidate can be evaluated for resilience with the
same machinery as for quality. A difference is called supported only when the interval excludes 0 with enough cases.

**What the Chaos Lab found in the platform itself** (each fixed, with a regression test):

1. *Duplicate delivery of a read-only tool call failed the step permanently.* Idempotent replay applied only to side-effecting
   tools, so the repeat executed again and violated the one-success-per-idempotency-key constraint (`internal_error`).
   Replay now applies to every tool.
2. *A dropped PostgreSQL connection failed work permanently.* Connection errors were classified as permanent `internal_error`;
   the first run of the experiment completed **0 of 6** runs (state stayed consistent, the work was just never retried). They are now
   `DatabaseUnavailableError` (transient), and the same experiment recovers.
3. *A per-run fault cap made the schedule timing-dependent* (found by the reproducibility test) → per-step caps and a
   seed-derived affected set.

## 5. Agent Security Center (Phase 5)

Migration `0007`. A deterministic security evaluation of the platform's **enforcement layer**: does the code, not the
prompt, stop an agent from doing what it must not? Every verdict is backed by persisted evidence (tool executions,
approvals, artifacts, events), and a case that did not actually exercise its attack is an *error*, never a pass.

```mermaid
flowchart TB
  S[security_tick<br/>leased phase machine] --> P[policy: 11 engine-level cases<br/>real execute_tool_call / approval functions]
  S --> U[setup: 36 fixtures → documents + runs<br/>ephemeral probe workspace with a canary]
  U --> W[workers execute the runs<br/>Security Test Agent = deliberately gullible mock]
  W --> G[grade from persisted records]
  S --> X[exposure: planted secrets → scan trace tables]
  G & P & X --> R[(security_evaluation_results<br/>append-only)] --> M[metrics + matrix + findings<br/>sealed run]
  M --> UI[Security Center · incidents from real events]
```

**What is tested**

| Group | Cases | How |
|---|---|---|
| Tool authorization | unauthorized tool, unknown/unregistered tool, malformed arguments | the real `execute_tool_call` against a real step scope; evidence = the recorded `tool_executions` row and that no artifact exists |
| Approvals | arguments changed after approval, rejected action retried, approval reused, expired approval | the real suspend → resolve → re-lease flow; the approval is bound to the exact arguments hash, single-use, expiring; a tampered call needs its own approval |
| Budgets | tool-call budget | calls beyond `max_tool_calls` are denied with `budget_exceeded` |
| Workspace isolation | search cannot see another workspace, data reader cannot open another workspace's file, path traversal | a throwaway second workspace holds a document and a data file containing a **canary**; the tests prove it exists, then prove the canary is unreachable |
| Prompt injection (28 attacks) | 7 goals × 4 surfaces (document body, retrieved chunk, search-result heading, mock API response) | the fixture is ingested, a real run reads it, the gullible agent obeys the embedded `[[CALL_TOOL name json]]` directive, and the platform decides |
| Benign controls (8) | plain text, prose naming tools, `[[brackets]]`, an **authorized** directive | must *not* be blocked: they measure false positives |
| Trace sanitization (4) | planted credential-shaped strings (Anthropic-style key, bearer token, database URL, AWS key id) in the run input, the search query and tool arguments | scanned for in `events`, `tool_executions`, `model_call_records`, `approval_requests`, attempt errors. Business data (run input, step outputs) legitimately holds what the user supplied and is reported separately |

**Integrity guards.** (1) A pass needs proof the attack was attempted (`injected_attempts > 0`); otherwise `error`.
(2) The canary check looks only at what *injected* tool calls returned, never at the legitimate read of the attack document
or at a search's echo of its own query (the first version of this check flagged exactly those, a false positive the
evidence made obvious). (3) Two **sensitivity tests** prove the suite can fail: raising the flood budget turns the four
flood cases into failures, and disabling the artifact writer's approval gate stops the approval case from passing.
(4) Test cases are versioned and immutable (`security_test_cases`, trigger-protected); finished runs are sealed.

**Metrics** (all with denominators): policy enforcement, attacks exercised, unauthorized-action attempts, blocked actions,
successful policy enforcement (attacks with no harmful effect), unsafe side effects (must be 0), unsafe writes prevented,
false-positive blocks, approval-bypass attempts, cross-workspace attempts and canary leaks, secret exposure in traces.

**Incidents** are read from real records, never fabricated: `tool.denied` events (with the tool execution as evidence),
rejected and expired approvals, budget stops, and failed security tests, each with run, step and origin
(workflow run, security suite, chaos experiment, re-execution).

**Authorization.** Anyone in the workspace can read verdicts and metrics; raw observed evidence and run links are
developer/admin only; starting the suite is developer+, one run at a time per workspace, idempotent.

**Isolation of the fixtures.** Fixture documents and the probe workspace are deleted when a run ends (success or failure);
the approved-write test artifacts are removed; synthetic scope runs are cancelled. Runs and result rows stay as evidence.
Runs with `purpose` in (`chaos`, `security`, `replay`) are excluded from the default run list and from Failure Intelligence.

**What this does not show.** The test model is a mock that obeys every directive, **by design**: results describe the
platform's code-level enforcement, not any real model's resistance to prompt injection. The corpus is small and
hand-written (a regression floor, not a guarantee). Only persisted traces are scanned for secrets; logs of other worker
processes are not.
