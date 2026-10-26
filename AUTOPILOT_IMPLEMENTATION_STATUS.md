# AgentOS Autopilot: implementation status

Legend: ✅ implemented and tested · 🟡 implemented, partially verified · ⏭️ not done

| Phase | Scope | Status |
|---|---|---|
| 0 Audit | Repository audit, gaps, existing coverage (AUTOPILOT_ARCHITECTURE.md §1) | ✅ |
| 1 Versioning | Immutable content-addressed `agent_config_versions`; step pinning; manual edits create versions; backfill migration; DB immutability triggers | ✅ migration tested upgrade → downgrade → upgrade → `check` on a restore of the real dev database (21 runs preserved, hashes match) |
| 2 OpenEval | Standalone `packages/openeval`: dataset schema, 6 deterministic evaluators, advisory LLM judge, metrics with denominators, paired bootstrap + McNemar, JSON/CSV/Markdown reports; 3 versioned dataset splits | ✅ |
| 3 Evaluation runs | Cases as real workflow runs with version overrides; budget and concurrency caps; `not_run` accounting; exports; hidden answers withheld from viewers and the optimizer | ✅ |
| 4 Failure intelligence | Taxonomy (infrastructure vs agent); 10 deterministic detectors with fingerprint dedupe; clusters; Diagnosis Agent with evidence-id validation, `hypothesis`/`invalid` status, standard limitation | ✅ mock · 🟡 real provider not run |
| 5 Optimizer | 2–5 candidates; `validate_candidate` restrictions in code; leakage detection; rejected candidates persisted; manual candidates | ✅ mock · 🟡 real provider not run |
| 6 Experiments & gates | Interleaved paired arms, trials, bootstrap CI, McNemar, per-case regressions, 9 gates, `improvement_demonstrated` separate from `eligible`, paid-run confirmation | ✅ |
| 7 Deployment | Promotion requests (eligible + non-stale baseline), admin approval with TTL, canary bucket, rollback to previously deployed versions only, monitoring with regression events, optional auto-rollback | ✅ · 🟡 canary and auto-rollback covered by tests only, not by demos |
| 8 Providers | Autopilot calls through existing adapters (Anthropic `claude-haiku-5-5` at low effort, OpenAI with explicit model); per-cycle spend limit; usage recorded with `purpose`; failures raise, never fall back to mock; opt-in live smoke test | 🟡 code and tests written; **no live call made** (no credentials) |
| 9 Dashboard | Autopilot overview, Failures, Improvements, Evaluations, Experiments (list + detail with charts, CI, gates, per-case table, diff), Deployments; live event feed (SSE with polling fallback); role-aware controls | ✅ Playwright 2 new tests (7/7 total) |
| 10 Demos, benchmarks, docs | 3 demos (`scripts/autopilot_demo.py`), benchmark + generated report and charts, 4 AUTOPILOT docs, README, Makefile, CI step | ✅ · 🟡 CI not run on GitHub |

## Compatibility with existing AgentOS

* **API.** All existing endpoints keep their request and response shapes. Additions:
  * `AgentOut` gains `active_version_id`, `canary_version_id` and `canary_percent`.
  * `StepOut` gains `agent_version_id`.
  * `GET /runs` gains an optional `purpose` filter.
  * The evaluation harness workflow is hidden from `/workflows`, and its name is reserved.
* **Data.** The migration is additive. It backfills v1 and an `initial` deployment for every
  existing agent and leaves old run histories untouched; steps without a pin fall back to the
  agent row.
* **Behavior.** `PATCH /agents/{id}` still applies edits immediately; it now also records a
  version and a `manual_edit` deployment. Runs created before an edit keep their pinned
  version.
* **Workspace default.** Users with several workspaces (the seed now adds the "Autopilot Lab"
  test workspace) default to their earliest workspace when `X-Workspace-Id` is absent. That
  is the same workspace as before for existing users.

## What remains

See AUTOPILOT_KNOWN_LIMITATIONS.md. The most important items:

1. A live run of the Diagnosis Agent and Optimizer on a real provider.
2. A larger, harder benchmark where a real model's quality is uncertain.
3. A two-person approval rule.
4. Sequential-testing-based monitoring.
