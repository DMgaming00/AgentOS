# AgentOS 2.0: audit of the existing system

Written 2026-10-26 at the start of the 2.0 upgrade, **before** any 2.0 feature work. Claims in the
spec's "previously reported" list are treated as unverified until reproduced here.

## 1. Baseline reproduction

| Claim carried into the upgrade | Reproduced? | Evidence (this session) |
|---|---|---|
| 190 backend tests pass, 3 skipped | **Yes** | `pytest -p no:warnings -rs -q`: `190 passed, 3 skipped in 94.88s`. Skips = 3 opt-in live-provider tests |
| Migrations apply, round-trip and have no drift | **Yes** | `upgrade head` → `downgrade base` → `upgrade head` → `check`: "No new upgrade operations detected" |
| 24/24 Autopilot demo checks, 20/20 original demo checks | **Yes** | Docker stack rebuilt, both scripts exit 0 (24 and 20 `PASS` lines) |
| 7/7 Playwright | **Yes** | `7 passed (30.6s)` against the rebuilt stack |
| ruff, strict mypy, eslint, tsc, `next build` | **Yes** | clean (107 → 109 source files after 2.0 Phase 1) |

Nothing is committed: the repository has no commits (`git log` is empty), so there is no history to
diff against; baseline numbers above are from the working tree.

## 2. The intermittent hang in `test_plan_endpoint_with_mock`

**Symptom.** During the previous session the API suite once ran for >400 s without completing, with
the last printed test being `test_plan_endpoint_with_mock`; later runs passed in ~20 s.

**Finding.** The test itself is clean: the endpoint holds no locks, uses the mock provider and a
request-scoped session. The cause is **test-infrastructure isolation**, not application code:

* `conftest.py` recreates the shared database `agentos_test` once per *session*, first running
  `pg_terminate_backend` on every connection to it and then `DROP DATABASE`.
* Two pytest sessions overlapped: a background run that had timed out was still alive when a second
  full run started (visible in the shell history: the first command was "moved to the background",
  then a second `pytest` was launched). The second session dropped the database under the first,
  whose pooled connections were then terminated mid-test. That presents as a stall in whichever test
  is running, and the default `signal` timeout does not fire inside a blocked asyncpg wait.
* Three subsequent runs, none overlapping another session, did not reproduce it (single test, first
  six tests, full API suite 20/20 in 20.5 s; later full suite 190 passed).

**Confidence.** Probable root cause, supported by the timeline and by the mechanism; I did not
deliberately reproduce it by racing two sessions.

**Fix applied (2.0 Phase 0).** A per-database advisory file lock in `conftest.py`: a second session
now fails immediately with `another pytest session is already using database 'agentos_test'` instead
of destroying the first. Verified by holding the lock from another process.

**A second, worse test-infra defect found while doing this.** If the migration failed, the session
fixture converted *any* exception into `pytest.skip`, so the whole integration suite would silently
"skip" (green) instead of failing. This hid a real migration error during Phase 1 development (a
wrong constraint name). Migration failures now raise and fail the session; only an unreachable
database skips.

## 3. What exists (inventory)

**Engine.** PostgreSQL is the source of truth. Steps are claimed with `FOR UPDATE SKIP LOCKED` under
lease tokens; heartbeats renew leases; every completion/failure/checkpoint/side-effect checks the
lease token (fencing). Retry with exponential backoff and jitter, retry deadlines, transient vs
permanent classification, run deadlines, cooperative cancellation, per-run concurrency, atomic budget
reservation, idempotency keys, per-step conversation checkpoints, approval gates bound to argument
hashes. Real-process crash tests exist (worker killed with exit 137).

**Workflow model (`domain/dag.py`).** Node types `agent_task`, `tool_task`, `approval_gate`,
`aggregation`; explicit `depends_on`; per-node retry policy; per-workflow budget
(`max_model_calls`, `max_tool_calls`, `max_duration_seconds`, `max_cost_usd`, `max_concurrency`);
Kahn cycle detection; limits.

**Agent runtime and providers.** Bounded loop with checkpoints and output contracts; Anthropic and
OpenAI adapters (official SDKs, tested with a mocked HTTP transport only); deterministic mock provider.

**Autopilot (previous upgrade).** Immutable content-addressed agent versions pinned per step;
OpenEval package; failure taxonomy and detectors; evidence-validated diagnosis; policy-constrained
optimizer; paired experiments with bootstrap CI and gates; admin-approved deployment, canary bucket,
rollback, monitoring; per-workspace Autopilot event stream; six dashboard pages.

**Telemetry.** Persisted per-run `events` (monotonic `sequence_number` per run, unique constraint,
`trace_id`), SSE with `Last-Event-ID` replay, OTel spans, `model_usage` with a versioned price table.

**Schema.** 30 tables across migrations `0001` (initial) and `0002` (autopilot).

**Frontend.** Next.js app: Overview, Agents, Workflows, Runs (live DAG, step panel, timeline),
Approvals, Artifacts, Observability, Settings, and six Autopilot pages. Existing dark theme and
component set.

## 4. Gaps against the 2.0 specification

| Spec area | Current state | Gap |
|---|---|---|
| **Offline guarantee (§0)** | The mock was the default but nothing *prevented* a real call: with a key in the environment, a real adapter would connect | **Closed in Phase 1** (see §5) |
| 2.1 DAG | No conditional branches, no fan-out beyond static `depends_on`, no nested workflows, no priority/starvation protection, no pause/resume (only cancel), no per-step deadline beyond `timeout_seconds` | Large: each needs scheduler and schema work |
| 2.3 Fault injection | `node.inject` exists for crash/rate-limit/timeouts in demos; no seeded, probabilistic, boundary-level injection | Chaos Lab |
| 2.4 Scheduling benchmarks | Throughput, parallelism, crash recovery, overhead benchmarks exist (1-4 workers, 5 and 20 node DAGs) | Need 8 workers, queue-wait, memory, DB contention |
| 3 Flight Recorder | Events + checkpoints exist; sanitized *model requests/responses* are not persisted; no playback UI, no timeline diff, no export | Whole feature |
| 4 Chaos Lab | none | Whole feature |
| 5 Security Center | Policy enforcement and tests exist (`test_security.py`); no adversarial benchmark, metrics, or dashboard | Mostly new |
| 6 Memory Lab | none | Whole feature |
| 7 Arena | OpenEval + one dataset family (33 cases); no leaderboard, no tool-use/retrieval families | New datasets + UI |
| 8 Optimizer 2.0 | Rule-less mock optimizer (scripted for the demo agent); no deterministic trace-driven rule engine | New rule engine |
| 9 FinOps | `model_usage` and a versioned price table; no budgets per agent/workspace, no anomaly detection, no suggestions | Extend |
| 10 Release Center | Canary percent (arbitrary), manual rollback, simple monitoring | Environments, preset canary steps, gates for security/budget, automatic rollback rules |
| 11 Observability | Metrics endpoints and one dashboard | Latency distributions, heatmaps, worker health |
| 12 Frontend | Flat nav; no grouped navigation | Reorganize |

## 5. Architectural risks found

1. **Silent permission to spend money (closed).** Any workflow whose agent says `provider: anthropic`
   plus a key in the environment would send a real request. Closed by the execution guard, two
   independent layers plus a spend ledger (`docs/AGENTOS_2_ARCHITECTURE.md` §1).
2. **Integration tests can pass by skipping (closed).** See §2.
3. **Autopilot's mock agents are scripted functions of their config** (see `AUTOPILOT_KNOWN_LIMITATIONS.md`).
   Arena and Optimizer 2.0 must not present those as intelligence benchmarks; labels are required in the UI.
4. **No persisted model request/response bodies.** The Flight Recorder cannot replay what the model
   saw without a new, sanitized capture path. Recorded replay depends on that landing first.
5. **Single-writer event sequence per run** (row lock). It is correct and benchmarked, but chaos and
   many-worker benchmarks will exercise it; expect contention to be the first measured bottleneck.
6. **`get_session` commit timing** was fixed in the previous session (commit before response); keep the
   guard test.

## 6. Upgrade plan (phases, with the acceptance check for each)

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | This audit; test-infra hardening | Baseline reproduced; concurrent-session lock verified |
| 1 | Execution modes, guarded adapters, fixture provider, spend ledger, isolation tests, offline indicator | Tests prove zero outbound connections with keys present; full suite green; migration round trip |
| 2 | Scheduler: priority + anti-starvation, pause/resume, conditional branches, bounded fan-out, nested workflows with depth limit; fault-injection and durability tests; 8-worker benchmarks | Existing 190 tests still pass; new crash-recovery tests |
| 3 | Flight Recorder: sanitized request/response capture, recorded replay, deterministic re-execution, timeline diff, export, UI | Replay never executes tools; export contains no secrets (tested) |
| 4 | Chaos Lab | Seeded faults reproduce identical outcomes |
| 5 | Security Center | Adversarial fixtures; metrics with denominators |
| 6 | Arena + datasets | Hidden-split protections tested |
| 7 | Memory Lab | Stale-memory evaluation |
| 8 | Optimizer 2.0 rule engine | Rules fire from trace evidence only |
| 9 | FinOps | Budget reservation enforced in code |
| 10 | Release Center | Canary routing and rollback rules tested |
| 11 | Unified UX | Grouped navigation, Run Inspector |
| 12 | Integration, 5 demos, benchmarks | All offline |
| 13 | Docs, README | Numbers generated from CSV |

The spec is very large. Phases are implemented in order and reported against what actually runs;
anything not finished is listed as not done in `AGENTOS_2_IMPLEMENTATION_STATUS.md`, never as done.
