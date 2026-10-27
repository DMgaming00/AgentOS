# AgentOS 2.0: test results

Recorded 2026-10-26 on an Apple M1 (8 GB), Python 3.13.7, Node 24.11, PostgreSQL 16.15, Redis 7 (Docker).
Every result below was produced by the command shown. **No real AI provider was called at any point.**
Updated at the end of each phase; the latest entry is at the bottom.

## Phase 0: baseline before any 2.0 code change

| Command | Result |
|---|---|
| `uv run pytest -p no:warnings -rs -q` | `190 passed, 3 skipped in 94.88s` |
| alembic upgrade / downgrade / upgrade / check | clean |
| `scripts/demo.py all`, `scripts/autopilot_demo.py all` (Docker) | exit 0; 20 and 24 `PASS` lines |
| `npx playwright test` | 7 passed |

## Phase 1: offline guarantee

```
uv run pytest -p no:warnings -rs -q
216 passed, 3 skipped in 87.03s     (190 baseline + 26 new)
```

| New suite | Tests | What it proves |
|---|---|---|
| `packages/shared/tests/test_offline_guard.py` | 15 | Defaults are offline. With fake Anthropic **and** OpenAI keys in the environment, `generate()` raises `provider_blocked_offline` while a socket spy and a real-transport spy record **zero** outbound connections and **zero** requests. Calling `provider.client` directly is also blocked. Each of the four flags is individually required (parametrized). All four together unblock the policy. Mock and fixture are always allowed. An in-memory contract transport is allowed offline. A live-mode 503 raises `ProviderUnavailableError`, not mock text. A missing key raises, never falls back. The spend cap is enforced against the ledger |
| `packages/shared/tests/test_fixture_provider.py` | 8 | Substring, digest and `fixture_id` matching; deterministic call ids; missing fixture fails loudly; recorded entries are redacted and round-trip by digest; digest ignores call ids and raw payloads; sets must be `simulated` and have unique ids |
| `apps/worker/tests/test_offline_engine.py` | 3 | **Through the real engine with real workers:** an agent configured for `anthropic` or `openai`, keys present, offline: the run fails, the step error is `provider_blocked_offline`, exactly 1 attempt (permanent, not retried), the recorded usage rows belong to the real provider with status `failed` (no mock rows), and 0 outbound connections. A `fixture` agent runs end to end and is priced `simulated` |

Test-infrastructure changes verified: a second pytest session now fails immediately with a clear message
(verified by holding the lock from another process); a failing migration now fails the session instead of
skipping every integration test (this surfaced and fixed a wrong constraint name in migration `0003`).

| Command | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean / 171 files formatted |
| `mypy` (core, api, worker, openeval; strict) | no issues in 109 source files |
| eslint, `tsc --noEmit` | clean |
| alembic upgrade → downgrade 0002 → upgrade → downgrade base → upgrade → check (fresh DB) | all succeeded; "No new upgrade operations detected" |
| Docker rebuild; `GET /api/v1/system` | `execution.mode: offline`, `paid_requests_allowed: false`, 4 blockers listed |
| `scripts/demo.py all` / `scripts/autopilot_demo.py all` | exit 0 / exit 0 (20 and 24 `PASS`) |
| `npx playwright test` | 7 passed (the viewer spec now also asserts the "Offline: paid providers blocked" banner) |

Not run: any live provider test (by design), the GitHub Actions workflow.

---

## Phases 2 and 3: engine v2 and the Flight Recorder

Recorded 2026-10-26, same machine and services as above. No real provider was called.

```
uv run pytest -p no:warnings -rs -q
298 passed, 3 skipped in 174.77s     (216 after Phase 1  +  82 new)
```

| New suite | Tests | What it proves |
|---|---|---|
| `packages/shared/tests/test_dag_v2.py` | 34 | All 11 condition operators; unresolvable refs and type errors are false, not exceptions; valid branching / fan_out / sub_workflow definitions; 13 invalid definitions rejected with the right issue (missing template, template with `depends_on`, non-task template, `max_items` > 20, unknown tool / agent in a template, refs to undeclared dependencies, `item` refs outside templates, unknown operator); aging arithmetic |
| `apps/worker/tests/test_engine_v2.py` | 22 | **Branches** (3 modes): only the chosen branch runs, skipped steps are never claimed and have no output, skips propagate, `any_succeeded` joins, run outputs ignore skipped branches. **Fan-out**: children run in parallel and results fan in in input order; `max_items` fails or truncates as configured; non-list / unresolved `over` fails; item text containing `{{...}}` stays literal; empty list succeeds; a failing child fails the node and cancels the rest. **Nested workflows**: child run with parent link, depth, inherited priority and a deadline ≤ the parent's; depth limit enforced (4 runs, deepest fails `depth_exceeded`, failure propagates up); missing workflow fails; cancelling the parent cancels a running child. **Pause/resume**: nothing claimed while paused, a running step finishes but the next is blocked, pausing a parent pauses its child, idempotent. **Priority**: higher priority first even if newer, FIFO within a priority, a waiting priority-0 run outranks a fresh priority-9 run (aging), paused runs never block others |
| `apps/worker/tests/test_engine_v2_recovery.py` | 4 | **Real worker processes** killed mid-step: a crash inside a child run (the parent's `waiting_child` step survives, the rescuer finishes the child, the parent completes, the engine-handled step was never leased); a crash after fan-out children finished (children attempt_count stays 1, only the crashed step is retried); SIGKILL while four fan-out children run (each recovered once, no duplicate children, results complete); a pending approval survives a killed worker and no second approval is created |
| `packages/shared/tests/test_flight_unit.py` | 9 | Capture redacts secrets everywhere, drops provider-raw payloads, truncates oversize requests visibly, keeps a digest of the real request; structured diff finds branch / retry / tool / model / output differences and reports identical runs as identical; JSON-path diff; state reconstruction; export redacts and is hash-verified; **export fails closed** when redaction is sabotaged |
| `apps/worker/tests/test_flight.py` | 9 | Every model call is recorded in the same transaction as its usage (count equality) and referenced by its event; recorded rows cannot be updated or deleted (DB trigger); recordings are contiguous and reconstruct the exact final state; failures are highlighted; **re-execution is side-effect free**: new run, same agent versions, zero mock/provider calls, zero artifacts, zero approvals, the unsafe tool refused and recorded, and the only divergent requests are the writer's two (smaller tool list; denial instead of artifact); a tool_task for an unsafe tool is refused; re-execution of unfinished or replay runs is refused; the recorded provider fails loudly when asked for more calls than recorded; structured comparison pinpoints the writer step; export of a real run holds no credentials |
| `apps/api/tests/test_flight_api.py` | 3 | Viewers can play back (flight, state at a frame, model-call detail) but not re-execute or export; idempotent re-execution; `purpose=replay` filter; compare; export is sanitized; **cross-workspace** access to every flight endpoint returns 404; `test_api.py` gained priority / pause / resume / validation coverage |

Playwright (Docker stack): **9 passed** (43.5 s): 7 existing + 2 new. The new `flight.spec.ts` plays, steps back and forward, scrubs to frame 0
(all nodes `pending`), plays to the end, opens a node's sanitized model calls, performs a deterministic re-execution,
lands on the labeled re-execution run and opens the comparison; a second test checks that a viewer can replay but sees
no re-execute or export controls.

| Command | Result |
|---|---|
| `ruff check .` / `ruff format --check .` | clean / 198 files formatted |
| `mypy` (core, api, worker, openeval; strict) | no issues in 118 source files |
| eslint, `tsc --noEmit`, `next build` | clean; new routes `/flight`, `/flight/[id]`, `/flight/compare` |
| alembic 0003 → 0004 → 0005: upgrade, downgrade, upgrade, downgrade base, upgrade, `check` | all succeeded; "No new upgrade operations detected" |
| Real dev data: migrations applied to the existing database | version `0005`; 912 existing runs preserved |
| `scripts/demo.py all` / `scripts/autopilot_demo.py all` | exit 0 (20 `PASS`) / exit 0 (24 `PASS`) |

### Defects and findings from this work

* **Benchmark invalidated by editing source while it ran** (my own error). Worker processes spawn from the working
  tree, so edits made mid-benchmark changed the code later cells ran against and made a cell hang (a model column that
  the benchmark database did not have). The partial results were discarded. Benchmarks now run from a frozen
  `git worktree` of a committed revision.
* **Scaling numbers are very sensitive to ambient load.** The same benchmark cell varied by more than 3× depending on
  whether the compose app containers were running; a control experiment (old engine code vs new, identical harness,
  containers stopped) showed the v2 engine is not slower (see `AGENTOS_2_BENCHMARKS.md`).
* **Prompt nondeterminism found by replay** (see architecture §3): upstream dependency outputs were assembled in
  database row order; now in declared order.
* **Test infrastructure**: a failing migration no longer turns the whole integration suite into skips; a second
  concurrent pytest session now fails fast instead of dropping the first session's database.

## Phase 4–5 verification (2026-10-26)

| Check | Result |
|---|---|
| Full backend suite (`uv run pytest`) | **348 passed, 3 skipped**, 280 s |
| `ruff check` / `ruff format --check` | clean (224 files) |
| `mypy --strict` | clean (131 source files) — one error found in the new security runner and fixed |
| Alembic | `upgrade head` on dev data (0006 → 0007 → 0008), `downgrade 0006` + `upgrade head` round trip, `alembic check`: no drift |
| Original demos (`scripts/demo.py all`) | all checks passed |
| Autopilot demo (`make autopilot-demo`) | all checks passed (42.7 s) |
| Playwright (Docker stack) | **14 passed** (adds chaos ×3, flight ×2, security ×2) |

Notes: 3 skipped tests are the pre-existing opt-in/live-provider tests. Earlier in development one Autopilot Playwright
spec failed once immediately after a rebuild and passed on rerun and in every later full run; the cause was not determined.

## Phase 6 verification (2026-10-26)

Full backend suite 363 passed, 3 skipped; ruff, `mypy --strict` (138 files) and `alembic check` (migration 0009) clean; Playwright 16/16; Autopilot demo passes.
The Autopilot Playwright spec failed twice during this phase and the cause was real, not flakiness: the new Arena datasets were picked up by split-only dataset lookups
(experiment dialog default, optimizer examples). Fixed and re-verified; the earlier unexplained single failure after a rebuild may have a similar cause but was never reproduced.

## Phase 7 verification (2026-10-26)

Backend suite 375 passed, 3 skipped (last full run); ruff, `mypy --strict` (145 files), `alembic check` (migration 0010, round trip) clean; Playwright memory spec 2/2.
**Flaky tests, not fixed:** across four full-suite runs during this phase, `test_chaos.py::test_a_dropped_postgres_connection_is_retried_not_fatal` failed in two and
`test_engine_budget.py::test_failure_cancellation_and_completion_release_unsettled_reservations` in one; both pass in isolation, in the worker-directory run and in the last full run.
Both are timing-sensitive and the runs overlapped with a rebuilt Docker stack and other CPU load; the cause was not isolated and no code change was made for them.
Bugs found by the new tests: refusal audits were rolled back with the failing transaction (now written in their own transaction); the browser could not call `PUT`/`DELETE` (CORS allow-list).
