# AgentOS 2.0: known limitations

Updated each phase. Items are things that are not done, not verified, or weaker than they may look.

## Verification gaps (carried over)

* No real Anthropic or OpenAI request has ever been made; the real adapters are verified only through the
  official SDKs with an in-memory transport. The opt-in live tests have never been run.
* CI has not run on GitHub; its steps were run locally.
* Repository has no git history prior to the 2.0 upgrade.

## Phase 1 (offline guarantee)

* The guard covers AgentOS's own model-request paths. It is not an OS-level network sandbox; code outside
  those paths could open a socket.
* The guarded transport replaces the SDK default transport, so proxy environment variables are not honored
  by real clients (documented in `docs/ENABLE_REAL_CLAUDE.md`).
* The spend cap is an application-level ledger check before each request, not a provider-side limit, and an
  in-flight request is counted only after it returns. Concurrent workers can overshoot by up to
  (workers × reservation) in a race.
* The fixture provider matches by digest or substring; it does not simulate streaming or provider quirks.
* The hang in `test_plan_endpoint_with_mock` is attributed to overlapping pytest sessions (probable, not
  deliberately reproduced); see `docs/AGENTOS_2_AUDIT.md` §2.
