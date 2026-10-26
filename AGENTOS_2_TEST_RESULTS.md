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
