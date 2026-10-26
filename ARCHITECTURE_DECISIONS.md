# Architecture decisions

Short ADRs; each states the decision, why, and what it costs.

**ADR-001 PostgreSQL is the queue and the source of truth.** Runs, steps, leases,
attempts, approvals and events live in one database, and claims use
`FOR UPDATE SKIP LOCKED`. *Why:* every state change and its audit event commit
atomically, there is no dual-write problem with a broker, and lease fencing is a
`WHERE` clause. *Cost:* throughput is bounded by one database. Measured at about
150–220 no-op steps/s on a laptop.

**ADR-002 Redis only for wake-ups.** Notifications are published after commit.
Consumers re-read PostgreSQL and fall back to polling. *Why:* losing Redis must not
lose or reorder anything. *Cost:* up to one poll interval of extra latency when
Redis is down.

**ADR-003 Providers and tools live in the shared core package,** not under
`apps/worker` as first sketched. *Why:* the API needs the tool catalog for
validation and the provider for synchronous planning. Importing the worker package
from the API would invert dependencies. *Cost:* the shared package is the largest
one.

**ADR-004 Per-run lock ordering.** Any transaction that touches a run's
steps/events/approvals/counters locks the run row first, and claims use
`SKIP LOCKED` on it. *Why:* deadlock freedom by construction, gap-free per-run event
sequences, and simple reasoning about state. *Cost:* transitions within one run are
serialized. This was the measured bottleneck (ADR-010).

**ADR-005 Lease tokens as fencing tokens.** Every claim mints a UUID. Every write
from a worker is conditional on it. *Why:* a paused or partitioned worker can wake
up after reassignment, and its write must be a no-op. *Cost:* every write path
carries the token.

**ADR-006 At-least-once plus idempotency, not exactly-once.** Retried steps may
re-run; side effects are deduplicated by key and constraint, or gated by a human.
*Why:* exactly-once external effects are not achievable by a scheduler alone.

**ADR-007 Durable agent checkpoints.** The normalized conversation (with
provider-native raw blocks) is saved after every model turn and tool round. *Why:*
recovery and approval resumption must not repeat model calls or reorder thinking
blocks. *Cost:* checkpoint writes on every turn. Checkpoints are cleared on success.

**ADR-008 Approval binding.** An approval references one tool execution and the
SHA-256 of its canonical arguments. It is consumed with a conditional UPDATE and
expires. *Why:* prevents approving A and executing B, and prevents reusing an
approval.

**ADR-009 A deterministic mock that consumes real tool output.** *Why:* CI and demos
need no keys and stay reproducible, while document search, calculator and artifact
paths still run for real. It is always labeled simulated, and a real provider is
never silently replaced by it.

**ADR-010 Batched event writes (measured optimization).** Events are buffered per
transaction and written with one sequence-block `UPDATE` and one insert at commit.
*Why:* run-lock wait accounted for 79–91% of active backends on fan-out workloads.
The A/B showed +11.6% to +22.1% median throughput. It can be disabled with
`AGENTOS_EVENT_BATCHING=false`.

**ADR-011 SSE over fetch, not EventSource or WebSockets.** *Why:* the token stays in
the `Authorization` header (never in the URL), resume works with `Last-Event-ID`,
and traffic is one-way and proxy-friendly.

**ADR-012 Generated TypeScript client.** OpenAPI -> `openapi-typescript` ->
`openapi-fetch`. Response schemas mark fields with defaults as required. *Why:* the
UI is typed against the real API, and CI fails if the schema drifts from the
committed copy.

**ADR-013 No default OpenAI model ID.** *Why:* the spec forbids guessing model IDs.
Anthropic's default (`claude-opus-5-5`) comes from current documentation, while the
OpenAI model must be configured explicitly.
