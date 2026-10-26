.PHONY: help install up down logs dev migrate seed test test-unit lint typecheck check web-check e2e demo demo-research demo-recovery demo-permissions autopilot-demo autopilot-demo-repair autopilot-demo-fake autopilot-demo-rollback autopilot-bench bench bench-quick bench-plot openapi screenshots

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

install:         ## install Python (uv) and web (npm) dependencies
	uv sync
	cd apps/web && npm ci

up:              ## build and start the full stack in docker (postgres, redis, api, 2 workers, web)
	docker compose up -d --build

down:            ## stop the stack (keeps volumes)
	docker compose down

logs:            ## follow api + worker logs
	docker compose logs -f api worker

dev:             ## run api/workers/web locally against docker postgres+redis
	docker compose up -d postgres redis
	./scripts/dev.sh

migrate:         ## apply database migrations
	uv run alembic -c apps/api/alembic.ini upgrade head

seed:            ## seed dev users, demo workspace, agents, documents and workflows
	uv run python -m agentos.seed

test:            ## all backend tests (needs: docker compose up -d postgres redis)
	uv run pytest -p no:warnings

test-unit:       ## backend + OpenEval unit tests only (no database)
	uv run pytest packages/shared/tests packages/openeval/tests -p no:warnings

lint:            ## ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

typecheck:       ## mypy --strict
	uv run mypy packages/shared/agentos_core apps/api/agentos apps/worker/agentos_worker packages/openeval/openeval

web-check:       ## frontend lint, typecheck and production build
	cd apps/web && npm run lint && npm run typecheck && npm run build

check: lint typecheck test web-check  ## everything CI runs (except e2e)

e2e:             ## Playwright against a running stack (make up first)
	cd apps/web && npx playwright test

demo:            ## run all three demos against the running stack
	uv run python scripts/demo.py all

demo-research:
	uv run python scripts/demo.py research
demo-recovery:
	uv run python scripts/demo.py recovery
demo-permissions:
	uv run python scripts/demo.py permissions

autopilot-demo:  ## Autopilot demos: repair, fake-improvement rejection, regression rollback (running stack)
	uv run python scripts/autopilot_demo.py all
autopilot-demo-repair:
	uv run python scripts/autopilot_demo.py repair
autopilot-demo-fake:
	uv run python scripts/autopilot_demo.py fake
autopilot-demo-rollback:
	uv run python scripts/autopilot_demo.py rollback

autopilot-bench: ## Autopilot optimization-cycle benchmark + report (isolated database, ~3 min)
	cd benchmarks && uv run --group bench python autopilot_bench.py --label autopilot --trials 3
	uv run --group bench python benchmarks/autopilot_report.py

bench:           ## full benchmark suite (several minutes; isolated database)
	cd benchmarks && uv run --group bench python run.py --label $${LABEL:-local}

bench-quick:
	cd benchmarks && uv run --group bench python run.py --label quick --quick

bench-plot:      ## render benchmark charts into docs/benchmarks
	uv run --group bench python benchmarks/plot.py

openapi:         ## regenerate openapi.json and the typed TS client
	uv run python scripts/export_openapi.py
	cd apps/web && npm run gen:api

screenshots:     ## capture UI screenshots from the running stack (run autopilot-demo first for Autopilot pages)
	cd apps/web && node tests/capture-screenshots.mjs
	cd apps/web && node tests/capture-autopilot.mjs
