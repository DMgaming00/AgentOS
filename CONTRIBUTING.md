# Contributing

```bash
make install                       # uv sync + npm ci
docker compose up -d postgres redis
make check                         # ruff, mypy --strict, pytest (real PostgreSQL), web lint/typecheck/build
make up && make e2e && make demo   # full stack, Playwright, demo checks
```

Guidelines:

* **State changes go through `agentos_core/engine/core.py`.** Lock the run row first
  (`lock_run`), then steps. Every worker-originated write must be fenced on the lease
  token. Add an integration test on PostgreSQL (never SQLite) for any new transition.
* **Schema changes:** edit `db/models.py`, then
  `uv run alembic -c apps/api/alembic.ini revision --autogenerate -m "…"`, review it,
  and confirm `alembic check` is clean.
* **API changes:** run `make openapi` to regenerate `apps/web/openapi.json` and the TS
  types. CI fails on drift.
* **New tools** declare a schema, side-effect class, timeout, idempotency and whether
  approval is required. Side-effecting tools must be idempotent under retry.
* **No real LLM calls in default tests.** Live tests go behind `AGENTOS_LIVE_TESTS=1`.
* Never commit secrets; `.env` is ignored.
