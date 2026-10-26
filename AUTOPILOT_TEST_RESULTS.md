# AgentOS Autopilot: test results

Recorded 2026-10-25 on an Apple M1 (8 GB, macOS 26.5), Python 3.13.7, Node 24.11,
PostgreSQL 16.15 and Redis 7 from `docker compose`. Every command below was run and its
output is summarized faithfully. **All model calls were made to the deterministic mock
provider.** No Anthropic or OpenAI API was called.

## Backend: `uv run pytest -p no:warnings -rs`

```
190 passed, 3 skipped in 81.99s
SKIPPED apps/worker/tests/test_live_autopilot.py: live provider tests are opt-in (AGENTOS_LIVE_TESTS=1)
SKIPPED apps/worker/tests/test_live_providers.py: ANTHROPIC_API_KEY not set
SKIPPED apps/worker/tests/test_live_providers.py: OPENAI_API_KEY and AGENTOS_OPENAI_MODEL not set
```

| Suite | Tests | Autopilot coverage |
|---|---|---|
| `packages/openeval/tests` (unit) | 14 | Each evaluator, including a citation that exists but was not retrieved, null expectations and tolerance edges. Judge refused without calibration. Metric denominators; `cost_per_successful_task` is `None` with no successes. Bootstrap determinism and CI direction; McNemar exact values; "not significant" below min n; JSON/CSV/MD reports |
| `packages/shared/tests` (unit) | 99 (26 Autopilot) | Config hashing and canonical form. `validate_candidate`: unknown/never-optimizable fields, tool additions, disallowed models, sampling params on Anthropic/OpenAI, step and retry limits, safety-weakening text, hidden-answer leakage, no-op. Taxonomy mapping, gates (hard vs soft, undefined cost per success, CI-based degradation), policy loading drops unknown fields, empty env vars treated as unset |
| `apps/api/tests` (integration) | 20 (4 Autopilot + 1 regression guard) | API-driven cycle with roles (viewer/developer 403s, admin approve) and idempotency-key replay; exports in 3 formats with hidden expected answers withheld from viewers; paid-run confirmation (409 without `confirm_cost`); cross-workspace 404s on Autopilot resources; policy changes are admin-only and cannot widen never-optimizable fields |
| `apps/worker/tests` (integration) | 60 (20 Autopilot) | **Versioning (9):** content-addressed versions from edits, sequential numbering, DB triggers reject UPDATE/DELETE, running and queued steps keep their pin, version retry policy, canary bucketing, legacy unpinned steps still run, config validation and diff. **Evaluation (6):** datasets split and hashed, dataset immutability, flawed agent fails with traceable errors, the prompt fix passes while skipping search fails grounding, expected answers never reach the agent, the budget cap marks the rest `not_run`. **Cycle (5):** full detect → diagnose → generate → experiment → gate → approve → deploy → monitor → rollback with the fake improvement refused; expired and stale promotion requests; development split rejected for experiments; diagnosis with invented evidence stored `invalid`; the per-cycle spend limit stops Autopilot calls |

## Opt-in live smoke test: **not run against a provider**

`apps/worker/tests/test_live_autopilot.py` checks the Diagnosis Agent and Optimizer contract
against a real provider. No credentials were available, so it was **not** run against
Anthropic or OpenAI. Its body was run once with the provider substituted by the mock
(`AGENTOS_LIVE_TESTS=1 AUTOPILOT_LIVE_PROVIDER=mock`), which passed. That only shows the test
itself works.

## Static checks

| Command | Result |
|---|---|
| `uv run ruff check .` / `ruff format --check .` | All checks passed / 161 files already formatted |
| `uv run mypy` (core, api, worker, openeval; strict) | Success: no issues found in 107 source files |
| `alembic upgrade head` → `downgrade base` → `upgrade head` → `check` (fresh database) | all succeeded; "No new upgrade operations detected" |
| The same round trip on a restore of the real dev database | 21 runs and 12 artifacts preserved; v1 + initial deployment created for every agent; backfilled hashes equal runtime hashes |
| `npm run lint` / `typecheck` / `build` | clean / clean / 22 routes built (6 new Autopilot pages) |

## Demos on the Docker stack (`docker compose up -d --build`)

`uv run python scripts/demo.py all` (the original demos): **20/20 checks passed.**

`uv run python scripts/autopilot_demo.py all`: **24/24 checks passed in 41.7 s.**

| Demo | Key measured results (simulated mock provider) |
|---|---|
| 1 Repair | Dev split 5/9 pass; failures classified `OUTPUT_SCHEMA_INVALID: 4`. Diagnosis: hypothesis, medium confidence, 6 evidence refs. The tool-adding candidate was rejected in code. Validation success 43.8% → 100%, paired diff +0.562, CI95 [0.3125, 0.8125], n=16 → eligible, improvement demonstrated. The developer approval was refused; the admin approved; v2 is active; a new live run uses v2 and succeeds |
| 2 Fake improvement | Candidate total cost $0.00870 vs $0.04895, but success fell 43.8% → 0.0% with 7 per-case regressions. Not eligible (per_case_regressions, no_supported_quality_degradation, minimum_success_rate, cost_per_successful_task); the promotion request was refused (409) |
| 3 Regression and rollback (Autopilot Lab workspace) | v2 live 5/5 succeeded. The max_steps=2 candidate was eligible offline with no improvement claimed. v3 live 5/5 failed (step limit); monitoring flagged a 0% → 100% failure rate (n=5). The developer rollback was refused; the admin rolled back to v2; new runs succeed; runs made during the regression stay pinned to v3 |

## Playwright (`npx playwright test`, against the Docker stack)

```
7 passed (30.6s)
```

The 5 existing specs pass, plus 2 new ones:

* `autopilot.spec.ts`: diagnose → generate (3 cards, one `rejected_policy`) → run the
  experiment → eligible with CI chart, gates, diff and zero regressions → request → admin
  approves in the Deployments page → active version changes → rollback with a reason → the
  overview event feed shows `version.rolled_back`.
* A viewer sees every Autopilot page but no mutating controls.

## Benchmark

`benchmarks/autopilot_bench.py --trials 3` ran in 114 s. The generated report is
[docs/autopilot-benchmarks.md](docs/autopilot-benchmarks.md).

## Defect found and fixed during the final regression

The first Docker run of the original recovery demo failed with a `404` on
`GET /runs/{id}/steps` immediately after `POST …/runs` returned `201`. The cause was in the
base system, not Autopilot: FastAPI 0.162 ends a `yield` dependency *after* the response by
default, so `get_session` committed after the client already had the new run id. It is fixed
with `Depends(get_session, scope="function")` (commit before the response), plus a guard test.
After the fix, all demos, Playwright and the full suite were rerun with the results above.

During one intermediate run the API suite hung in `test_plan_endpoint_with_mock`. It did not
reproduce in three subsequent runs (the single test, the first 6 tests, and the full suite:
20/20 in 20.5 s). The cause was not identified. It may have been contention with an earlier,
interrupted test process.

## Not run

* Any live Anthropic or OpenAI call (no credentials).
* The GitHub Actions workflow (only its steps were run locally).
* Canary rollouts and auto-rollback through a demo (both are covered by integration tests).
