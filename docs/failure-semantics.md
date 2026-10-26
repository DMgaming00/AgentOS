# Failure semantics

## What is guaranteed

| Property | Mechanism | Test |
|---|---|---|
| A step is never executed by two workers *with a valid lease* at the same time | Claim in one transaction with `FOR UPDATE SKIP LOCKED`; a fresh `lease_token` per claim | `test_concurrent_claims_never_double_claim` |
| A worker that lost its lease cannot change the step | Every write checks the current `lease_token` inside the run-locked transaction | `test_stale_worker_cannot_overwrite_newer_result` |
| Completed steps are never re-executed | Recovery only reclaims `running` steps with expired leases; terminal states have no outgoing transitions | `test_worker_crash_is_recovered_without_rerunning_completed_steps` |
| Crashed work is recovered | Lease expiry -> `retry_wait` (backoff) -> `ready` -> claimed by any worker | same, plus Benchmark 3 (18/18 recovered) |
| A side effect approved once is performed at most once per idempotency key | Approval consumption is a conditional UPDATE; artifacts have `UNIQUE (run_id, idempotency_key)`; a succeeded tool execution is replayed, not re-run | `test_crash_after_side_effect_does_not_duplicate_artifact`, `test_artifact_writer_idempotent_under_duplicate_dispatch` |
| Per-run events are gap-free and ordered | Sequence block reserved under the run lock; `UNIQUE (run_id, sequence_number)` | `test_event_sequence_is_contiguous_and_ordered` |
| Budgets cannot be overspent by concurrent calls | Reservation is one atomic `UPDATE ... WHERE model_calls < max AND spent + reserved + est <= max` | `test_model_call_budget_reservation_is_atomic` |

## Delivery: at-least-once, not exactly-once

A worker can crash after doing work but before recording it. The step is then run
again. AgentOS therefore gives **at-least-once execution** of retryable steps.
Exactly-once *external* effects cannot be guaranteed by a scheduler alone. They need
the external system to cooperate (idempotency keys, conditional writes) or a human.

What AgentOS does about it:

* **Agent conversations are checkpointed** after every model turn and every tool
  round (fenced). A retried agent step resumes from the last checkpoint, so it does
  not repeat earlier model calls. The model call in flight at the moment of the crash
  *is* repeated and billed again.
* **Tools declare their side-effect class** (`none`, `internal`, `external`). For
  side-effecting tools, a deterministic idempotency key
  `sha256(run_id, node_key, tool, canonical_args)` is checked before execution, and
  a prior success is returned as `idempotent_replay`.
* **The artifact writer** is fully idempotent. The insert is guarded by a unique
  constraint, the file path is content-addressed, and the write is fenced, so a stale
  worker cannot write.
* **External actions** (none ship in v1) must use an idempotency-capable adapter or
  stay approval-gated, so a human handles any ambiguous retry.

## Retry policy

* Transient (`retryable = True`): provider rate limit, timeout, 5xx/overload,
  connection errors, worker lost (lease expired), step timeout.
* Permanent: invalid input, policy violation / unauthorized tool, budget exceeded,
  provider auth or bad request, invalid model output (after one repair turn),
  approval rejected, step limit, model refusal, unknown exceptions.
* Backoff: `min(cap, base * 2^(n-1))` with full jitter. The retry is skipped if
  it would land past the node's `retry.deadline_seconds`. `failure_count` counts
  failures; resuming after an approval is a new attempt but not a failure.
* Provider SDK retries are disabled (`max_retries=0`), so every retry is a visible,
  fenced attempt in the history.

## Failure of a step

Fail-fast per run. When a step fails permanently (or exhausts retries), every step
that has not started is marked `skipped`, and steps waiting for approval are
`cancelled` with their approvals expired. Steps already running finish, and their
results are kept. The run becomes `failed` once nothing is in flight. The run error
lists the failed steps and the first error.

## Cancellation

`POST /runs/{id}/cancel` takes the run lock and sets the run `cancelled`
immediately. Pending, ready, retry-wait and approval-waiting steps become
`cancelled`, pending approvals expire, and an event lists the steps still in flight.
Workers learn about it from their next heartbeat (or a Redis wake-up) and cancel the
asyncio task cooperatively. A step that finishes anyway, for example inside a
non-interruptible tool call, is recorded as `cancelled` with its output kept
(`completed_after_cancellation`). `completed_at` is set once the last in-flight step
drains.

## Timeouts and budgets

* Per step: `timeout_seconds` (asyncio timeout, transient -> retried).
* Per agent: `max_steps` model turns, `max_cost_usd` per step, provider timeout.
* Per run: `max_model_calls`, `max_tool_calls`, `max_cost_usd`, `max_concurrency`,
  `max_duration_seconds` (the coordinator fails the run at its deadline).
* Reservation before each model call, then reconciliation against reported usage.
  If usage is not reported, the reservation is charged and the cost basis is marked
  `unknown`/`partial`.

## Known gaps

* Lease expiry has one TTL per deployment. A legitimately slow step that blocks the
  event loop for longer than the TTL can lose its lease. The retry is then safe but
  wasteful.
* Time comes from each host's clock for lease deadlines written by workers, and from
  PostgreSQL's `now()` for validity checks. Large clock skew between hosts shortens
  or lengthens effective leases.
* A model call that was in flight during a crash is billed twice.
