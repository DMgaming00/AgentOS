# Demo B: Chaos recovery report (run 8026f182-3849-47c0-83a7-23f7b5651c30)

_Deterministic engineering demonstration on the offline mock provider (simulated tokens and prices); it shows the platform's behavior, not the intelligence of any real model._

Experiment `5c634a0b-c7cb-41c7-b1fb-5a77d5e3d275`, seed 11, plan hash `eacb6a0feded0df3`. Faults: provider_rate_limit, provider_timeout, worker_crash.

## Scorecard
- completion: 3/3
- injections: 15; recovery rate 1.0; attempts per step 2.8333333333333335
- time to detection (median): 25.695 ms; time to recovery (median / p95): 17216.794 / 34819.468 ms
- duplicate side effects prevented: 0/0

## Timeline of run `8026f182-3849-47c0-83a7-23f7b5651c30`

| time (UTC) | what happened |
|---|---|
| 06:09:45.947 | attempt `attempt 1` of `r0` starts on `39d54fda462d-1-8c7511` (queue wait 28 ms) |
| 06:09:45.947 | attempt `attempt 1` of `r1` starts on `39d54fda462d-1-8c7511` (queue wait 28 ms) |
| 06:09:45.947 | attempt `attempt 1` of `r2` starts on `39d54fda462d-1-8c7511` (queue wait 28 ms) |
| 06:09:46.090 | **fault injected**: `provider_timeout` at `before_model_call` in step `r1` (attempt 1) -> None |
| 06:09:46.110 | **fault injected**: `worker_crash` at `before_model_call` in step `r0` (attempt 1) -> None |
| 06:10:01.021 | attempt ends: **lease_expired** (worker_lost) |
| 06:10:01.065 | attempt ends: **lease_expired** (worker_lost) |
| 06:10:01.074 | attempt ends: **lease_expired** (worker_lost) |
| 06:10:01.092 | attempt `attempt 2` of `r0` starts on `39d54fda462d-1-225723` (queue wait 5 ms) |
| 06:10:01.182 | **fault injected**: `provider_timeout` at `before_model_call` in step `r0` (attempt 2) -> None |
| 06:10:01.206 | attempt ends: **failed** (provider_timeout) |
| 06:10:02.282 | attempt `attempt 2` of `r1` starts on `39d54fda462d-1-225723` (queue wait 8 ms) |
| 06:10:02.282 | attempt `attempt 2` of `r2` starts on `39d54fda462d-1-225723` (queue wait 8 ms) |
| 06:10:02.282 | attempt `attempt 3` of `r0` starts on `39d54fda462d-1-225723` (queue wait 8 ms) |
| 06:10:02.339 | **fault injected**: `worker_crash` at `before_model_call` in step `r1` (attempt 2) -> None |
| 06:10:18.115 | attempt ends: **lease_expired** (worker_lost) |
| 06:10:18.170 | attempt ends: **lease_expired** (worker_lost) |
| 06:10:18.181 | attempt ends: **lease_expired** (worker_lost) |
| 06:10:19.347 | attempt `attempt 3` of `r1` starts on `44ee126ba0b7-1-425c0b` (queue wait 23 ms) |
| 06:10:19.347 | attempt `attempt 3` of `r2` starts on `44ee126ba0b7-1-425c0b` (queue wait 23 ms) |
| 06:10:19.347 | attempt `attempt 4` of `r0` starts on `44ee126ba0b7-1-425c0b` (queue wait 23 ms) |
| 06:10:19.507 | **fault injected**: `provider_timeout` at `before_model_call` in step `r2` (attempt 3) -> None |
| 06:10:19.511 | attempt ends: **succeeded** |
| 06:10:19.540 | attempt ends: **failed** (provider_timeout) |
| 06:10:19.556 | attempt ends: **succeeded** |
| 06:10:20.568 | attempt `attempt 4` of `r2` starts on `44ee126ba0b7-1-425c0b` (queue wait 13 ms) |
| 06:10:20.614 | **fault injected**: `worker_crash` at `before_model_call` in step `r2` (attempt 4) -> None |
| 06:10:35.852 | attempt ends: **lease_expired** (worker_lost) |
| 06:10:37.061 | attempt `attempt 5` of `r2` starts on `44ee126ba0b7-1-aae4e0` (queue wait 9 ms) |
| 06:10:37.189 | attempt ends: **succeeded** |
| 06:10:37.201 | attempt `attempt 1` of `agg` starts on `44ee126ba0b7-1-aae4e0` (queue wait 7 ms) |
| 06:10:37.219 | attempt ends: **succeeded** |

Final status: **succeeded** after 13 attempts across 4 steps.

Replay it: http://localhost:3000/flight/8026f182-3849-47c0-83a7-23f7b5651c30  ·  inspect it: http://localhost:3000/runs/8026f182-3849-47c0-83a7-23f7b5651c30?view=trace
