# Test results

Recorded 2026-10-25 on an Apple M1 (8 GB, macOS 26.5), Python 3.13.7, Node 24.11,
PostgreSQL 16.15 and Redis 7 from `docker compose`. Every command below was run, and
its output is summarized faithfully. Nothing here is projected.

## Backend: `uv run pytest -p no:warnings -rs`

```
125 passed, 2 skipped in 40.45s
SKIPPED apps/worker/tests/test_live_providers.py: ANTHROPIC_API_KEY not set
SKIPPED apps/worker/tests/test_live_providers.py: OPENAI_API_KEY and AGENTOS_OPENAI_MODEL not set
```

| Suite | Result | Covers |
|---|---|---|
| `packages/shared/tests` (unit, no DB) | 73 passed | DAG validation (Kahn cycles, missing/duplicate keys, unauthorized/unknown tools, refs, limits), state transitions, backoff + retry classification, pricing (actual/partial/unknown/simulated), calculator and its safe parser, path traversal, redaction, chunking, tsquery sanitization, **Anthropic and OpenAI adapters through the real SDKs with a mocked HTTP transport** (request shape, thinking-block replay, tool-result grouping, refusal fallback beta, usage normalization, error mapping), mock determinism, planner bounded replanning and policy |
| `apps/api/tests` (integration, real PostgreSQL) | 15 passed | auth, workspace isolation (404s), roles, agent CRUD validation, workflow versioning, plan endpoint, run idempotency keys, a full API flow with in-process workers (approval idempotency, artifact download + SHA-256, contiguous events, SSE replay with Last-Event-ID, metrics consistent with run counters), cancel, planned runs, uploads, 413 body limit, 429 rate limit, no secret leakage, OpenAPI |
| `apps/worker/tests` (integration, real PostgreSQL) | 37 passed, 2 skipped | concurrent claims (15 claimers x 30 steps, no double claim), per-run concurrency, dependency resolution, **stale-worker fencing**, heartbeat renew/lost/foreign lease, backoff, attempts exhausted, lease expiry, cancellation of queued and running steps, event ordering, deadlines, atomic budget reservation; end-to-end research workflow; parallel overlap; reject -> resubmit -> approve -> one artifact; approval argument binding and single use; approval gate + expiry; duplicate-approval constraint; provider rate-limit retry; timeout exhaustion; cost budget; invalid/unauthorized tool calls; artifact idempotency under duplicate dispatch; AI-planned runs (valid, invalid-then-valid, invalid); **operation with Redis down**; cooperative cancellation; **real worker processes killed mid-step (exit 137)**; crash after side effect without duplication; production safety checks; prompt injection cannot grant tools; data-reader confinement |

The two live-provider tests are opt-in by design (`AGENTOS_LIVE_TESTS=1` plus keys).
**No real Anthropic or OpenAI call was made in this environment.**

## Static checks

| Command | Result |
|---|---|
| `uv run ruff check .` / `ruff format --check .` | All checks passed / 112 files already formatted |
| `uv run mypy --strict` (core, api, worker) | Success: no issues found in 78 source files |
| `alembic upgrade head`, `downgrade base`, `upgrade head`, `check` | all succeeded; "No new upgrade operations detected" |
| `npx eslint .` | 0 problems |
| `npx tsc --noEmit` (strict) | passed |
| `npx next build` | compiled; 15/15 routes generated |

## End-to-end: `npx playwright test` against `docker compose up`

```
✓ rejecting an export on the approvals page fails the run without writing anything
✓ a failed run shows its error, retries and attempt history
✓ viewer can inspect but not approve
✓ start workflow, watch DAG and events live, approve, download, survive refresh
✓ plan a workflow from natural language, inspect DAG and validation
5 passed (11.2s)
```

The same suite also passed against the non-container local stack (`scripts/dev.sh`).

## Demos: `uv run python scripts/demo.py all` against `docker compose up`

All 20 checks passed: research report, failure recovery (worker container killed
and restarted by its restart policy, step reclaimed by the other worker), and
permission enforcement (rejection without side effect, resubmission, decided
approval reuse -> 409, crash after the write -> exactly one artifact via idempotent
replay, unauthorized tool denied).

## Not run

* The GitHub Actions workflow (`.github/workflows/ci.yml`): there is no remote
  repository from this environment.
* Live provider smoke tests: no credentials.
* The optional observability profile (OTel collector + Jaeger) was not started.
  Span creation is exercised by the tests (trace ids are recorded on events), but
  export to a collector was not verified.
