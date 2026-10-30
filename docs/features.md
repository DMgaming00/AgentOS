# Features, labs and engineering notes

Moved from the main README.

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
* **Autopilot:** see [above](../AUTOPILOT_ARCHITECTURE.md). Pages:
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

## Example workflows

| Demo | Shows | Run |
|---|---|---|
| [Research report](../examples/research-report/README.md) | 3 parallel research agents -> analysis with the calculator -> writer -> approval -> Markdown artifact with citations, conflicts and missing evidence | `make demo-research` |
| [Failure recovery](../examples/failure-recovery/README.md) | A worker process is killed mid-step; lease expiry; another worker retries; completed steps are not re-run | `make demo-recovery` |
| [Permission enforcement](../examples/approval-workflow/README.md) | Reject (no side effect) -> resubmission -> approve -> crash right after the write -> exactly one artifact; unauthorized tool denied | `make demo-permissions` |

Presenter notes: [docs/demo-script.md](demo-script.md).

## API

REST under `/api/v1` with OpenAPI at `http://localhost:8000/docs`. It provides
idempotency keys for run creation and approvals, pagination, and an SSE stream per
run. See [docs/api.md](api.md).

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
failure. To enable real Claude later, see [docs/ENABLE_REAL_CLAUDE.md](ENABLE_REAL_CLAUDE.md).

### How the offline results should be read

* Mock and fixture providers are **scripted**: an agent's score measures how its configuration interacts with the
  scripted policy, not what a real model would do. The leaderboard and experiment pages label this.
* Costs are **simulated** from a versioned price table and labeled as estimates, never invoices.
* The benchmarks run from a frozen `git worktree`, with the app containers stopped, an isolated database and real
  worker processes; where a claim compares versions, a control experiment (same harness, only code differs) is run.
* Several benchmark findings changed the code (a rollback rule with a 28-37% false-positive rate, 4 Chaos Lab bugs,
  tracing that started new traces in workers). They are recorded rather than hidden.

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
