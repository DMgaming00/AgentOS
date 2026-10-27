# Showcase demonstrations

Five end-to-end demonstrations driven through the public API against a running stack (`docker compose up -d --build`, then
`docker compose exec api python -m agentos.seed`). Each prints PASS/FAIL checks and exits non-zero on the first failure.

> These are **deterministic engineering demonstrations on the offline mock provider** (simulated tokens and prices). They show
> that the platform's machinery behaves as designed; they are not benchmarks of real-model intelligence. No paid API is called and
> no key is needed.

| Demo | Run | What it shows (every claim is a check against stored records) | Time |
|---|---|---|---|
| **A** Self-improving agent | `make showcase-a` | a flawed agent fails a structured task (5/9) → Failure Intelligence → evidence-linked diagnosis → the deterministic rule engine proposes a fix citing the failed steps → paired OpenEval experiment on held-out cases (43.8% → 100%, CI excludes 0) → a cheaper-but-worse candidate is refused → promotion only after admin approval → new runs use it | ~25 s |
| **B** Chaos recovery | `make showcase-b` | a 3-branch workflow under seeded worker crashes and provider timeouts/rate limits: faults injected at real boundaries, all runs recover, state stays consistent, a gap-free Flight Recorder recording, and a recovery timeline report generated from the stored records ([demo_b_recovery_report.md](demo_b_recovery_report.md)) | ~55 s |
| **C** Security enforcement | `make showcase-c` | an untrusted document tells an obedient agent to write a file; the agent tries; AgentOS denies it in code, nothing is written, a durable `tool.denied` event and a Security Center incident are recorded; then the full 51-case security evaluation (28/28 attacks contained, 0 false positives) | ~10 s |
| **D** Cost optimization | `make showcase-d` | an agent that asks for every search twice (answers correct, 9/9); FinOps reports the repeated activity without claiming savings; the rule engine proposes duplicate-call protection; a held-out experiment shows correctness preserved while model-requested tool calls drop 32 → 16 and estimated cost per successful task falls ~29% (simulated pricing) | ~20 s |
| **E** Canary and rollback | `make showcase-e` | a `max_steps=2` candidate passes offline evaluation but fails a live multi-subject workload; behind gates, an admin-approved 50% canary with seeded routing; the failure-rate rule fires with its evidence window and AgentOS rolls back by itself; new runs use the restored version; canary-era runs stay pinned to what they ran | ~80 s |

`make showcase` runs all five. Each demo creates fresh agents and records (history is immutable), so they can be re-run.

Numbers above are from the run recorded in `AGENTOS_2_TEST_RESULTS.md`; they are deterministic for A, C, D (mock provider) and bounded for B and E (fault timing and routing vary with the clock and run ids).
