#!/usr/bin/env bash
# Run the full stack locally (without docker for app processes): API, N workers and the web UI.
# Requires: docker compose up -d postgres redis ; uv ; npm.  Logs go to var/logs/.
set -euo pipefail
cd "$(dirname "$0")/.."
WORKERS="${WORKERS:-2}"
mkdir -p var/logs var/artifacts
uv run alembic -c apps/api/alembic.ini upgrade head
uv run python -m agentos.seed
pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
AGENTOS_LOG_JSON=false uv run uvicorn agentos.main:app --port 8000 >var/logs/api.log 2>&1 & pids+=($!)
# Workers are supervised like docker's restart policy, so the crash demos can kill one safely.
for i in $(seq 1 "$WORKERS"); do
  (while true; do
     AGENTOS_LOG_JSON=false uv run agentos-worker --id "local-worker-$i-$RANDOM" >>"var/logs/worker-$i.log" 2>&1 || true
     sleep 1
   done) & pids+=($!)
done
(cd apps/web && NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://localhost:8000}" npm run dev >../../var/logs/web.log 2>&1) & pids+=($!)
echo "API http://localhost:8000/docs · Web http://localhost:3000 · logs in var/logs/ (Ctrl-C to stop)"
wait
