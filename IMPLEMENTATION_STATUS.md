# Implementation status

Legend: ✅ implemented and tested · 🟡 implemented, partially verified · ⏭️ deferred

| Phase | Scope | Status |
|---|---|---|
| 0 Environment | uv workspace (core/api/worker), Next.js app, Docker Compose (postgres, redis, migrate+seed, api, 2 workers, web, optional otel/jaeger), Makefile, lint/type/test tooling, `.env.example` | ✅ the stack starts with `make up`; health and readiness checks pass |
| 1 Database & core API | 17 tables, one migration with drift check, workspaces/roles, agent CRUD, immutable workflow versions, run creation, central state machines | ✅ |
| 2 Durable scheduler | DAG validation, dependency resolution, SKIP LOCKED claims, leases, heartbeats, fencing, retries with backoff/jitter/deadline, lease-expiry recovery, cancellation, run deadlines, per-run concurrency | ✅ includes real-process crash tests |
| 3 Agent runtime & models | Provider protocol; mock (10 scenarios); Anthropic (Messages API, refusal fallback); OpenAI (Responses API, stateless); bounded loop with checkpoints; output contracts; planner with bounded replanning; usage accounting | ✅ mock end to end · 🟡 Anthropic/OpenAI verified against the SDKs with a mocked transport only, never against the live API (no keys) |
| 4 Tools & approvals | Registry; calculator; document search (PostgreSQL FTS); structured data reader; artifact writer; approval state machine; API; idempotency | ✅ · ⏭️ HTTP fetch (by design, see security.md) |
| 5 Events & observability | Persisted ordered events, SSE with replay, OTel spans, usage and cost with a versioned price table, metrics endpoints, trace correlation | ✅ · 🟡 OTLP export to a collector not exercised |
| 6 Frontend | Shell, overview, agents, workflows (planner/editor/validation/DAG), run detail (live DAG, step panel, timeline, artifacts, events), approvals, artifacts, observability, settings/documents; loading, empty and error states; SSE reconnection | ✅ Playwright 5/5 |
| 7 Demos | Research report, failure recovery, permission enforcement: sample data, scripts, verified checks | ✅ local and Docker |
| 8 Benchmarks | Four suites, CSV results, charts, generated tables, bottleneck analysis, one optimization with A/B | ✅ |
| 9 Hardening | Security tests, production startup checks, docs, screenshots, CI workflow | ✅ · 🟡 CI not run on GitHub |

## Hardened vs experimental

* **Hardened by tests:** claim/lease/fencing logic, recovery, approvals and their
  binding, tool policy, idempotent artifacts, event ordering, budgets, workspace
  isolation.
* **Experimental:** real-provider behavior (adapter code untested against live
  APIs), development authentication, the AI planner quality with real models,
  single-database scaling.
