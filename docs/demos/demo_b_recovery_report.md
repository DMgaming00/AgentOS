# Demo B: Chaos recovery report (run 9d26518d-305e-496a-83a0-c6e68336c84e)

_Deterministic engineering demonstration on the offline mock provider (simulated tokens and prices); it shows the platform's behavior, not the intelligence of any real model._

Experiment `3d555b3c-329f-43a9-adbc-2c59bc512641`, seed 11, plan hash `eacb6a0feded0df3`. Faults: provider_rate_limit, provider_timeout, worker_crash.

## Scorecard
- completion: 3/3
- injections: 13; recovery rate 1.0; attempts per step 2.75
- time to detection (median): 35.059 ms; time to recovery (median / p95): 17170.03 / 34257.767 ms
- duplicate side effects prevented: 0/0

## Timeline of run `9d26518d-305e-496a-83a0-c6e68336c84e`

| time (UTC) | what happened |
|---|---|
| 10:30:31.455 | attempt `attempt 1` of `r0` starts on `4c4816b583ae-1-694909` (queue wait 13 ms) |
| 10:30:31.455 | attempt `attempt 1` of `r1` starts on `4c4816b583ae-1-694909` (queue wait 13 ms) |
| 10:30:31.455 | attempt `attempt 1` of `r2` starts on `4c4816b583ae-1-694909` (queue wait 13 ms) |
| 10:30:31.542 | **fault injected**: `worker_crash` at `before_model_call` in step `r0` (attempt 1) -> None |
| 10:30:47.048 | attempt ends: **lease_expired** (worker_lost) |
| 10:30:47.079 | attempt ends: **lease_expired** (worker_lost) |
| 10:30:47.094 | attempt ends: **lease_expired** (worker_lost) |
| 10:30:48.192 | attempt `attempt 2` of `r0` starts on `4c4816b583ae-1-a3e5b4` (queue wait 15 ms) |
| 10:30:48.192 | attempt `attempt 2` of `r1` starts on `4c4816b583ae-1-a3e5b4` (queue wait 15 ms) |
| 10:30:48.239 | attempt `attempt 2` of `r2` starts on `b2472200997c-1-e6f381` (queue wait 61 ms) |
| 10:30:48.356 | **fault injected**: `worker_crash` at `before_model_call` in step `r1` (attempt 2) -> None |
| 10:30:48.361 | **fault injected**: `provider_timeout` at `before_model_call` in step `r2` (attempt 2) -> None |
| 10:30:48.411 | attempt ends: **failed** (provider_timeout) |
| 10:30:50.328 | attempt `attempt 3` of `r2` starts on `4c4816b583ae-1-a3aecf` (queue wait 12 ms) |
| 10:30:50.493 | attempt ends: **succeeded** |
| 10:31:04.205 | attempt ends: **lease_expired** (worker_lost) |
| 10:31:04.237 | attempt ends: **lease_expired** (worker_lost) |
| 10:31:05.355 | attempt `attempt 3` of `r0` starts on `4c4816b583ae-1-a3aecf` (queue wait 20 ms) |
| 10:31:05.355 | attempt `attempt 3` of `r1` starts on `4c4816b583ae-1-a3aecf` (queue wait 20 ms) |
| 10:31:05.800 | attempt ends: **succeeded** |
| 10:31:05.833 | attempt ends: **succeeded** |
| 10:31:05.838 | attempt `attempt 1` of `agg` starts on `b2472200997c-1-e6f381` (queue wait 3 ms) |
| 10:31:05.885 | attempt ends: **succeeded** |

Final status: **succeeded** after 10 attempts across 4 steps.

Replay it: http://localhost:3000/flight/9d26518d-305e-496a-83a0-c6e68336c84e  ·  inspect it: http://localhost:3000/runs/9d26518d-305e-496a-83a0-c6e68336c84e?view=trace
