## Engine v2: scheduling, fan-out, priority, nested recovery

Machine: Apple M1, 8 cores, 8.0 GB RAM, macOS-26.5.2-arm64-arm-64bit-Mach-O, Python 3.13.7, postgres (PostgreSQL) 16.15 in Docker Desktop. Started 2026-10-26T14:47:29-0700, took 373.1 s. Real worker processes and an isolated database; every number is derived from rows the engine wrote (attempt timestamps, `ready_at`, `queue_wait_ms`) plus `psutil` samples of the worker processes and `pg_stat_activity` samples. **No model calls are made**: nodes are aggregations and calculator tool calls, so these measure the scheduler and database, not agents. Reproduce: `cd benchmarks && uv run --group bench python engine_v2_bench.py --label engine_v2`.

### Scaling with worker processes

![scaling](engine_v2_scaling.png)

| workers | DAG nodes | workflows | steps | exec/s | start latency p50 ms | p95 ms | RSS max MB | CPU mean % | lock-wait share |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 5 | 40 | 200 | 72.5 | 618.5 | 962.1 | 83.3 | 58.9 | 0.3 |
| 1 | 20 | 40 | 800 | 65.3 | 5853.0 | 8159.9 | 81.0 | 40.6 | 0.669 |
| 2 | 5 | 40 | 200 | 60.2 | 719.0 | 1167.0 | 164.5 | 96.8 | 0.65 |
| 2 | 20 | 40 | 800 | 61.9 | 2712.0 | 6185.0 | 162.9 | 67.1 | 0.751 |
| 4 | 5 | 40 | 200 | 33.4 | 634.5 | 2609.0 | 289.5 | 109.9 | 0.655 |
| 4 | 20 | 40 | 800 | 58.8 | 3860.0 | 8851.7 | 290.3 | 101.5 | 0.716 |
| 8 | 5 | 40 | 200 | 10.6 | 877.0 | 6378.0 | 447.0 | 94.4 | 0.292 |
| 8 | 20 | 40 | 800 | 17.8 | 14268.0 | 21173.0 | 504.5 | 64.2 | 0.647 |

The backlog is pre-filled before workers start, so *start latency* is queueing delay inside the backlog, not submission delay. *Lock-wait share* is the fraction of sampled active backends waiting on a PostgreSQL lock (the per-run row lock that serializes event sequencing and state transitions). *RSS* is summed over all worker processes. One repetition per cell: treat differences under ~15% as noise.

### Static DAG vs `fan_out` node

![fanout](engine_v2_fanout.png)

| kind | parallel tasks | reps | processing s (mean) | min | max |
|---|---|---|---|---|---|
| fan_out | 5 | 3 | 2.36 | 2.15 | 2.51 |
| static | 5 | 3 | 2.35 | 2.22 | 2.55 |
| fan_out | 18 | 3 | 6.34 | 5.47 | 7.06 |
| static | 18 | 3 | 5.54 | 4.75 | 6.10 |

The same number of calculator tasks, declared statically versus expanded by a `fan_out` node at run time. The engine-handled expansion adds one extra step row and one `step.fan_out` event per workflow; differences between the two within the min–max ranges are not significant.

### Priority aging (starvation protection)

![priority](engine_v2_priority.png)

| config | rep | high runs | low runs | low completion p50 s | low max s | high completion p50 s | high max s | low finished before backlog drained % |
|---|---|---|---|---|---|---|---|---|
| aging_off | 0 | 60 | 6 | 3.13 | 3.16 | 2.64 | 2.93 | 0.0 |
| aging_off | 1 | 60 | 6 | 3.03 | 3.09 | 2.63 | 2.88 | 0.0 |
| aging_off | 2 | 60 | 6 | 3.21 | 3.25 | 2.82 | 3.08 | 0.0 |
| aging_2s | 0 | 60 | 6 | 3.38 | 3.39 | 2.92 | 3.21 | 0.0 |
| aging_2s | 1 | 60 | 6 | 3.25 | 3.31 | 2.86 | 3.11 | 0.0 |
| aging_2s | 2 | 60 | 6 | 3.12 | 3.15 | 2.67 | 2.91 | 0.0 |

A backlog of priority-9 four-step chains is submitted with a few priority-0 chains. **Aging is accelerated to 2 s per priority point** (default 30 s) so the effect shows in a short benchmark; with aging off the low-priority runs wait for the entire backlog. The cost is the high-priority median completion shown above.

### Crash inside a nested workflow

![nested](engine_v2_nested_recovery.png)

| lease TTL s | run | parent | child | victim attempts | detect s | parent done after crash s | duplicate attempts |
|---|---|---|---|---|---|---|---|
| 2.0 | 0 | succeeded | succeeded | 2 | 14.118 | 14.907 | 0 |
| 2.0 | 1 | succeeded | succeeded | 2 | 1.894 | 2.638 | 0 |
| 2.0 | 2 | succeeded | succeeded | 2 | 1.919 | 2.648 | 0 |
| 2.0 | 3 | succeeded | succeeded | 2 | 1.847 | 2.554 | 0 |
| 2.0 | 4 | succeeded | succeeded | 2 | 1.829 | 2.561 | 0 |
| 2.0 | 5 | succeeded | succeeded | 2 | 1.947 | 2.669 | 0 |
| 5.0 | 0 | succeeded | succeeded | 2 | 5.07 | 5.838 | 0 |
| 5.0 | 1 | succeeded | succeeded | 2 | 4.348 | 5.068 | 0 |
| 5.0 | 2 | succeeded | succeeded | 2 | 4.823 | 5.505 | 0 |
| 5.0 | 3 | succeeded | succeeded | 2 | 4.978 | 5.859 | 0 |
| 5.0 | 4 | succeeded | succeeded | 2 | 5.278 | 6.058 | 0 |
| 5.0 | 5 | succeeded | succeeded | 2 | 4.694 | 5.732 | 0 |
| 10.0 | 0 | succeeded | succeeded | 2 | 9.123 | 9.854 | 0 |
| 10.0 | 1 | succeeded | succeeded | 2 | 9.548 | 10.364 | 0 |
| 10.0 | 2 | succeeded | succeeded | 2 | 9.19 | 10.32 | 0 |
| 10.0 | 3 | succeeded | succeeded | 2 | 10.064 | 10.861 | 0 |
| 10.0 | 4 | succeeded | succeeded | 2 | 9.489 | 10.173 | 0 |
| 10.0 | 5 | succeeded | succeeded | 2 | 9.393 | 10.161 | 0 |

A worker process is killed (`os._exit(137)`) while running the only step of a child run; the parent's `sub_workflow` step waits durably. *Detect* is crash → lease-expiry detection; *parent done* includes the retry in the rescuing worker and the coordinator's child→parent propagation tick.
