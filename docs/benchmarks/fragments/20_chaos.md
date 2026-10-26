## Chaos Lab: resilience scorecards

Machine: Apple M1, 8 cores, 8.0 GB, macOS-26.5.2-arm64-arm-64bit-Mach-O, Python 3.13.7, postgres (PostgreSQL) 16.15 in Docker. Started 2026-10-26T16:46:06-0700, took 165.2 s. 3 processes x concurrency 4, supervised, lease TTL 3 s, 12 runs per experiment, mock provider, **no model calls**. Each built-in scenario is run three times with the identical fault schedule: retries disabled (`no_retry`), the workflow's own retries (`as_configured`, 3 attempts) and `extra_retries` (5 attempts). These numbers describe how the *platform* responds to faults, not how a real model behaves. Reproduce: `cd benchmarks && uv run --group bench python chaos_bench.py --label chaos`.

### Completion with and without retries

![chaos](chaos_completion.png)

| scenario | retries | runs | injections | completed | recovery rate | attempts / step | time to detect (median) | time to recover (median / p95, n) | state consistency | errors seen |
|---|---|---|---|---|---|---|---|---|---|---|
| control (no faults) | none | 12 | 0 | 12/12 (100%) | n/a | 1.00 | n/a | n/a / n/a (n=0) | 100% | — |
| control (no faults) | as configured | 12 | 0 | 12/12 (100%) | n/a | 1.00 | n/a | n/a / n/a (n=0) | 100% | — |
| control (no faults) | 5 attempts | 12 | 0 | 12/12 (100%) | n/a | 1.00 | n/a | n/a / n/a (n=0) | 100% | — |
| provider-brownout | none | 12 | 22 | 0/12 (0%) | 0% | 0.60 | 30 ms | n/a / n/a (n=0) | 100% | provider_rate_limited;provider_timeout |
| provider-brownout | as configured | 12 | 32 | 12/12 (100%) | 100% | 1.67 | 25 ms | 830 ms / 1.46 s (n=32) | 100% | — |
| provider-brownout | 5 attempts | 12 | 32 | 12/12 (100%) | 100% | 1.67 | 31 ms | 921 ms / 1.63 s (n=32) | 100% | — |
| garbage-model-output | none | 12 | 39 | 12/12 (100%) | 100% | 1.00 | 113 ms | 288 ms / 396 ms (n=39) | 100% | — |
| garbage-model-output | as configured | 12 | 39 | 12/12 (100%) | 100% | 1.00 | 80 ms | 226 ms / 340 ms (n=39) | 100% | — |
| garbage-model-output | 5 attempts | 12 | 39 | 12/12 (100%) | 100% | 1.00 | 93 ms | 199 ms / 329 ms (n=39) | 100% | — |
| flaky-tools | none | 12 | 19 | 12/12 (100%) | 100% | 1.00 | 26 ms | 129 ms / 10.12 s (n=19) | 100% | — |
| flaky-tools | as configured | 12 | 19 | 12/12 (100%) | 100% | 1.00 | 22 ms | 114 ms / 10.17 s (n=19) | 100% | — |
| flaky-tools | 5 attempts | 12 | 19 | 12/12 (100%) | 100% | 1.00 | 19 ms | 119 ms / 10.06 s (n=19) | 100% | — |
| worker-crash-loop | none | 12 | 9 | 0/12 (0%) | 0% | 0.75 | 2.53 s | n/a / n/a (n=0) | 100% | worker_lost |
| worker-crash-loop | as configured | 12 | 15 | 12/12 (100%) | 100% | 1.94 | 2.89 s | 4.09 s / 7.68 s (n=15) | 100% | — |
| worker-crash-loop | 5 attempts | 12 | 19 | 12/12 (100%) | 100% | 2.40 | 3.10 s | 4.04 s / 8.42 s (n=19) | 100% | — |
| infrastructure-hiccups | none | 12 | 46 | 4/12 (33%) | 36% | 0.79 | 222 ms | 252 ms / 616 ms (n=32) | 100% | database_unavailable |
| infrastructure-hiccups | as configured | 12 | 71 | 12/12 (100%) | 100% | 1.38 | 223 ms | 408 ms / 1.48 s (n=71) | 100% | — |
| infrastructure-hiccups | 5 attempts | 12 | 81 | 12/12 (100%) | 100% | 1.81 | 214 ms | 530 ms / 3.67 s (n=81) | 100% | — |
| duplicate-delivery | none | 12 | 88 | 12/12 (100%) | 100% | 1.20 | 203 ms | 848 ms / 1.46 s (n=68) | 100% | — |
| duplicate-delivery | as configured | 12 | 90 | 12/12 (100%) | 100% | 1.20 | 256 ms | 1.02 s / 1.58 s (n=68) | 100% | — |
| duplicate-delivery | 5 attempts | 12 | 91 | 12/12 (100%) | 100% | 1.20 | 187 ms | 958 ms / 1.40 s (n=68) | 100% | — |
| hostile-tool-output | none | 12 | 10 | 12/12 (100%) | 100% | 1.00 | 126 ms | 224 ms / 250 ms (n=10) | 100% | — |
| hostile-tool-output | as configured | 12 | 10 | 12/12 (100%) | 100% | 1.00 | 124 ms | 229 ms / 258 ms (n=10) | 100% | — |
| hostile-tool-output | 5 attempts | 12 | 10 | 12/12 (100%) | 100% | 1.00 | 125 ms | 234 ms / 243 ms (n=10) | 100% | — |

### Is the retry effect real? (paired, per scenario)

| scenario | comparison | n | baseline | candidate | paired difference | 95% CI | McNemar p | supported? |
|---|---|---|---|---|---|---|---|---|
| control (no faults) | no_retry -> as_configured | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| control (no faults) | as_configured -> extra_retries | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| provider-brownout | no_retry -> as_configured | 12 | 0% | 100% | +1.000 | [+1.000, +1.000] | 0.0005 | True |
| provider-brownout | as_configured -> extra_retries | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| garbage-model-output | no_retry -> as_configured | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| garbage-model-output | as_configured -> extra_retries | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| flaky-tools | no_retry -> as_configured | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| flaky-tools | as_configured -> extra_retries | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| worker-crash-loop | no_retry -> as_configured | 12 | 0% | 100% | +1.000 | [+1.000, +1.000] | 0.0005 | True |
| worker-crash-loop | as_configured -> extra_retries | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| infrastructure-hiccups | no_retry -> as_configured | 12 | 33% | 100% | +0.667 | [+0.417, +0.917] | 0.0078 | True |
| infrastructure-hiccups | as_configured -> extra_retries | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| duplicate-delivery | no_retry -> as_configured | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| duplicate-delivery | as_configured -> extra_retries | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| hostile-tool-output | no_retry -> as_configured | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |
| hostile-tool-output | as_configured -> extra_retries | 12 | 100% | 100% | +0.000 | [+0.000, +0.000] | n/a | False |

A difference is labeled *supported* only when the paired bootstrap interval excludes 0 with at least 10 matched cases (OpenEval's rule); otherwise it is inconclusive, however large the raw difference looks.

### Reading the results

* Step retries are what rescue `provider-brownout`, `worker-crash-loop` and `infrastructure-hiccups` (completion rises from the *none* arm to the *as configured* arm and the paired interval excludes 0).
* `garbage-model-output` and `flaky-tools` completed in every arm with 1.00 attempts per step even though faults were injected, so those faults were absorbed *below* the step-retry layer (the injection ledger and Flight Recorder show where); retries were not what saved them.
* Going from 3 to 5 attempts changed no outcome in any scenario at this fault intensity. This benchmark cannot say whether 5 attempts helps under heavier faults; it only shows 3 were already enough here, at the cost of more attempts per step in the crash and infrastructure scenarios.
* 12 runs per cell is small: a result of 12/12 is compatible with a true rate well below 100%.

### Safety metrics

| scenario | duplicates neutralized | duplicate executions found | policy held under hostile output | state consistency checks | budget compliance |
|---|---|---|---|---|---|
| control (no faults) | 0/0 | 0 | n/a | 100% of 60 | 100% |
| provider-brownout | 0/0 | 0 | n/a | 100% of 60 | 100% |
| garbage-model-output | 0/0 | 0 | n/a | 100% of 60 | 100% |
| flaky-tools | 0/0 | 0 | n/a | 100% of 60 | 100% |
| worker-crash-loop | 0/0 | 0 | n/a | 100% of 60 | 100% |
| infrastructure-hiccups | 0/0 | 0 | n/a | 100% of 60 | 100% |
| duplicate-delivery | 89/89 | 0 | n/a | 100% of 60 | 100% |
| hostile-tool-output | 0/0 | 0 | 100% of 10 runs | 100% of 60 | 100% |

The duplicate and hostile-output rows are evidence that the idempotency fence and tool policy operated, not that a model is robust to prompt injection.
