# API

Base URL `http://localhost:8000`. The interactive OpenAPI UI is at `/docs`, and the
machine-readable schema at `/openapi.json`. That schema is also committed as
`apps/web/openapi.json`, from which the typed TypeScript client is generated
(`make openapi`).

## Conventions

* **Auth:** `Authorization: Bearer <jwt>` from `POST /api/v1/auth/login`. Every
  `/api/v1` endpoint except login requires it.
* **Workspace:** `X-Workspace-Id: <uuid>` selects the workspace. It is optional when
  the user belongs to exactly one.
* **Errors:** `{"error": {"code": "...", "message": "...", "details": ...}}` with
  401/403/404/409/413/422/429/503 as appropriate. Resources in other workspaces
  return 404.
* **Pagination:** `?limit=&offset=` returns `{items, total, limit, offset}`.
* **Idempotency:** `Idempotency-Key` on `POST /workflows/{id}/runs`,
  `POST /runs/planned` and approval decisions. A replay returns the stored response
  with `Idempotent-Replayed: true`. Reusing a key with a different body is a 422
  (`idempotency_key_reused`).
* **Limits:** 1 MB request bodies (10 MB uploads). Critical endpoints are
  rate-limited (`429` with `Retry-After`).

## Endpoints

| Method | Path | Role | Notes |
|---|---|---|---|
| GET | `/health`, `/ready` | – | readiness checks PostgreSQL (required) and Redis (optional) |
| POST | `/api/v1/auth/login` | – | dev auth; rate-limited |
| GET | `/api/v1/auth/me` | any | user and memberships |
| GET/POST | `/api/v1/agents` | read / developer | create validates tools against the registry |
| GET/PATCH | `/api/v1/agents/{id}` | read / developer | |
| GET | `/api/v1/agents/{id}/executions` | read | steps this agent ran |
| GET/POST | `/api/v1/workflows` | read / developer | POST with an existing name creates version n+1 |
| POST | `/api/v1/workflows/validate` | read | issues, topological order, levels |
| POST | `/api/v1/workflows/plan` | developer | AI planning (`provider`: mock/anthropic/openai); validated, not saved |
| GET | `/api/v1/workflows/{id}`, `/versions` | read | includes live validation against current permissions |
| POST | `/api/v1/workflows/{id}/runs` | developer | `{input}`; idempotent |
| POST | `/api/v1/runs/planned` | developer | `{task, input, max_cost_usd}`; DAG planned by a worker (status `planning`) |
| GET | `/api/v1/runs` | read | filters `status` (comma-separated), `workflow_id` |
| GET | `/api/v1/runs/{id}` | read | counters, step counts, duration |
| POST | `/api/v1/runs/{id}/cancel` | developer | 409 if already terminal |
| GET | `/api/v1/runs/{id}/steps` | read | steps with attempt history |
| GET | `/api/v1/runs/{id}/steps/{step_id}` | read | attempts, tool executions, model usage, events, approvals |
| GET | `/api/v1/runs/{id}/tool-executions`, `/usage`, `/artifacts` | read | |
| GET | `/api/v1/runs/{id}/events?after=` | read | persisted, ordered by `sequence_number` |
| GET | `/api/v1/runs/{id}/stream` | read | Server-Sent Events, see below |
| GET | `/api/v1/approvals` | read | filters `status`, `run_id` |
| GET | `/api/v1/approvals/{id}` | read | |
| POST | `/api/v1/approvals/{id}/approve`, `/reject` | developer | `{reason}`; idempotent; 409 if not pending (or expired before decision) |
| GET | `/api/v1/artifacts`, `/{id}`, `/{id}/download`, `/{id}/content` | read | download is an attachment; content is a text/plain preview |
| GET/POST | `/api/v1/documents`, `/{id}` | read / developer | multipart upload (.md .txt .pdf .csv .json) |
| GET | `/api/v1/tools` | any | registry with schemas and side-effect/approval policy |
| GET | `/api/v1/metrics/overview`, `/usage`, `/cost`, `/observability` | read | `?days=`; computed from persisted rows |
| GET/PATCH | `/api/v1/workspace` | read / admin | name, `max_run_cost_usd` |
| GET | `/api/v1/system` | any | providers configured (booleans), pricing version, limits, never secret values |

## Server-Sent Events

```
GET /api/v1/runs/{id}/stream
Authorization: Bearer ...
Last-Event-ID: 41          (or ?after=41)
```

```
retry: 2000

id: 42
event: approval.requested
data: {"id":..., "run_id":"...", "sequence_number":42, "event_type":"approval.requested", "payload":{...}, "trace_id":"..."}

: keepalive

event: stream.end
data: {"status":"succeeded","last_sequence":57}
```

Event types: `run.created`, `run.started`, `run.planned`, `step.ready`,
`step.started`, `model.call.started`, `model.call.completed`, `model.call.failed`,
`tool.started`, `tool.completed`, `tool.failed`, `tool.denied`,
`approval.requested`, `approval.resolved`, `step.retrying`, `step.failed`,
`step.completed`, `step.skipped`, `step.cancelled`, `step.suspended`,
`step.stale_result_rejected`, `run.waiting_approval`, `run.resumed`,
`run.completed`, `run.failed`, `run.cancelled`, `budget.exceeded`.

Events are committed before they are published. The stream replays from PostgreSQL
and re-reads on every Redis notification, polling once per second when Redis is
unavailable. Clients deduplicate by `sequence_number`. The browser uses
`fetch` streaming rather than `EventSource` so the bearer token stays in a header
and out of URLs.

## Example

```bash
TOKEN=$(curl -s localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
  -d '{"email":"admin@agentos.dev","password":"agentos-admin"}' | jq -r .access_token)
WF=$(curl -s localhost:8000/api/v1/workflows -H "authorization: Bearer $TOKEN" \
  | jq -r '.items[] | select(.name=="research-report") | .id')
RUN=$(curl -s -X POST localhost:8000/api/v1/workflows/$WF/runs -H "authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -H "Idempotency-Key: $(uuidgen)" -d '{}' | jq -r .id)
curl -N localhost:8000/api/v1/runs/$RUN/stream -H "authorization: Bearer $TOKEN"
```
