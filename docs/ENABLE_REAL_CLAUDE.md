# Enabling real Claude (later, deliberately)

Everything in AgentOS runs offline by default. **Do not follow this guide until you want to spend
money.** Until 2026-10-28 no real provider call had ever been made. On that date the Anthropic adapter was run once
against `claude-haiku-5-5` (97 calls, about $0.055; see the README's Real-model results). The OpenAI adapter has
never been run live, and the Anthropic one has had very little live use.

## What protects you

Paid-provider requests are refused in code (`agentos_core/providers/execution.py`) unless **all four**
settings below are set. A key in the environment is never enough. The check runs at the start of every
adapter call and again under the SDK's HTTP transport, so calling `provider.client` directly is blocked
too. A request that is refused raises `provider_blocked_offline` before any connection is opened, and
the run fails visibly; the platform never substitutes mock output for a real-provider failure.

| Setting | Offline default | Needed for live |
|---|---|---|
| `AGENTOS_EXECUTION_MODE` | `offline` | `live` |
| `AGENTOS_ALLOW_PAID_PROVIDERS` | `false` | `true` |
| `AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS` | `false` | `true` |
| `AGENTOS_MAX_REAL_API_SPEND_USD` | `0` | a small positive number |

The spend cap is checked **before each request** against the spend already recorded in `model_usage`
for real providers, plus that request's reservation (default $0.05). It is an application-level guard,
not a provider-side limit: set a monthly limit in the Anthropic console as well, and note that a request
already in flight is not counted until it returns.

The header banner shows `Offline: paid providers blocked` or `LIVE: paid providers enabled`.

## Setup, in order

1. **Add the key to a local, git-ignored file** (never commit it):
   ```bash
   cp .env.example .env
   echo 'ANTHROPIC_API_KEY=sk-ant-...' >> .env
   ```
   With the defaults, the app still refuses to call Claude. Confirm: `curl localhost:8000/api/v1/system`
   shows `execution.paid_requests_allowed: false` and lists the blockers.
2. **Choose a cheap supported model** for the first tests (price table: `packages/shared/agentos_core/pricing.json`):
   ```bash
   AGENTOS_ANTHROPIC_MODEL=claude-haiku-5-5
   AGENTOS_AUTOPILOT_ANTHROPIC_MODEL=claude-haiku-5-5
   ```
3. **Explicitly enable paid mode with a small cap** (example: 25 cents):
   ```bash
   AGENTOS_EXECUTION_MODE=live
   AGENTOS_ALLOW_PAID_PROVIDERS=true
   AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS=true
   AGENTOS_MAX_REAL_API_SPEND_USD=0.25
   ```
   Restart the API and workers (`docker compose up -d`). The banner must turn red.
4. **Run one opt-in provider smoke test** (one tool round trip, a few cents at most):
   ```bash
   AGENTOS_LIVE_TESTS=1 AGENTOS_EXECUTION_MODE=live AGENTOS_ALLOW_PAID_PROVIDERS=true \
   AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS=true AGENTOS_MAX_REAL_API_SPEND_USD=0.25 \
   AGENTOS_ANTHROPIC_MODEL=claude-haiku-5-5 ANTHROPIC_API_KEY=sk-ant-... \
   uv run pytest apps/worker/tests/test_live_providers.py -m live -rs
   ```
5. **One real agent workflow.** Create an agent with provider `anthropic`, model `claude-haiku-5-5`, run
   a one-node workflow, and open the run: the model call must show provider `anthropic`, provider-returned
   token counts, and cost basis `actual` (not `simulated`).
6. **One diagnosis + optimization cycle.** In the workspace policy set `diagnosis_provider` and
   `optimizer_provider` to `anthropic` (`PUT /api/v1/autopilot/policy`, admin), then:
   ```bash
   AGENTOS_LIVE_TESTS=1 AUTOPILOT_LIVE_PROVIDER=anthropic AGENTOS_EXECUTION_MODE=live \
   AGENTOS_ALLOW_PAID_PROVIDERS=true AGENTOS_ENABLE_NETWORK_MODEL_REQUESTS=true \
   AGENTOS_MAX_REAL_API_SPEND_USD=0.50 ANTHROPIC_API_KEY=sk-ant-... \
   uv run pytest apps/worker/tests/test_live_autopilot.py -m live -rs
   ```
   Claude-generated candidates pass exactly the same deterministic validation (`validate_candidate`),
   hidden-answer protections and paired evaluation as offline ones.
7. **A controlled evaluation on a small dataset.** Evaluation cost scales with
   cases × trials × 2 arms. Use `max_eval_trials=1`, the 8-case test split is *not* for selection; use
   validation with `trials=1` (32 runs). Start it from the UI; a paid run asks for confirmation above
   `paid_confirmation_threshold_usd`.
8. **Verify usage and cost.** Compare `model_usage.input_tokens/output_tokens` for the real calls with the
   usage page of the Anthropic console. The platform's number is an **estimate** from a bundled price table
   (`pricing.version`), not an invoice; differences from caching or price changes are expected.
9. **Raise limits only deliberately**, one setting at a time, and re-check the banner and the console.
10. **To go back offline**, set `AGENTOS_EXECUTION_MODE=offline` (or remove the flags) and restart.

## AgentOS 2.0 modules and real providers

* **Set a FinOps budget first.** Before step 3, create an enforcing workspace budget (FinOps, Budgets, admin) smaller than your
  `AGENTOS_MAX_REAL_API_SPEND_USD`. It stops a model call *before* it is made when recorded spend plus in-flight reservations
  would exceed it; it is a second, independent guard (the spend cap above is global, the budget is per workspace or agent and
  per day or month). Amounts remain estimates.
* **Per-lab real modes (all opt-in, admin only, refused unless the four paid-mode settings are on, counted against the
  spend cap):** Security Center "Run with real model" (`POST /security/runs?agent=live`, or `agent=live_naive` for a red-team prompt that tells the model to carry out the documents' instructions), Memory Lab "Run with real
  reader" (`POST /memory/experiments?reader=live`), Agent Arena runs with real contenders (`allow_live: true`, scripted by
  `scripts/real_haiku_arena.py`), and the Chaos Lab, which injects faults around whatever provider the workflow uses
  (`scripts/real_haiku_chaos.py`). Defaults stay offline, results are labeled "real model", and the automated test suite
  forces offline mode whatever your `.env` says, so a live `.env` cannot make a test spend money.
* **Deterministic checks apply to Claude output too:** Claude-proposed Autopilot candidates go through the same policy
  validation, paired held-out experiments, promotion gates (including the Security Center evaluation), canary stages and
  rollback rules as rule-generated ones. A higher model-written score never promotes anything.
* **Release Center first run:** keep `auto_rollback` on and `min_stage_samples` at or above 10 for the first real canary.

## Known caveats

* The real adapters were verified only against the official SDKs with a mocked HTTP transport.
* The guarded transport replaces the SDK's default transport, so `HTTP(S)_PROXY` variables are **not**
  honored by real clients. Add a proxy-aware transport if you need one.
* Model IDs and prices change; check `pricing.json` before trusting cost estimates.
* The OpenAI adapter additionally needs `OPENAI_API_KEY` and an explicit `AGENTOS_OPENAI_MODEL`.
