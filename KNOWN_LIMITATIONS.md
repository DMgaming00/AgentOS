# Known limitations

## Not verified in this environment

* **Live Anthropic and OpenAI calls.** The adapters were tested against the official
  SDKs with a mocked HTTP transport (request shape, parsing, error mapping), not
  against the live APIs, because no credentials were available. Opt-in smoke tests:
  `AGENTOS_LIVE_TESTS=1 ANTHROPIC_API_KEY=… uv run pytest -m live`.
* **OpenAI stateless replay** (`store=false` with encrypted reasoning items) follows
  the documented pattern but is unverified live. Non-reasoning models can set
  `model_configuration.reasoning=false` to skip `include=reasoning.encrypted_content`.
* **CI on GitHub** and **OTLP export** to the bundled collector/Jaeger were not run.

## Design limits

* **Throughput:** one PostgreSQL instance; transitions within a run are serialized
  by the run lock (about 150–220 no-op steps/s on the test laptop). Many concurrent
  runs scale better than one very wide run.
* **Delivery:** at-least-once. A model call in flight during a crash is repeated and
  billed twice. External side-effect tools would need their own idempotency.
* **Leases:** one TTL per deployment. A step that blocks the event loop longer than
  the TTL loses its lease (a safe but wasteful retry). Lease expiry compares
  timestamps written by worker clocks, so large clock skew between hosts matters.
* **Fail-fast runs:** a permanent failure skips all not-yet-started steps, including
  independent branches. There is no "continue on error" policy yet.
* **Cost estimates** depend on the bundled price table (version 2026-10-06, Anthropic
  only). Cache-write and Haiku cache-read rates are not bundled and fall back to the
  input rate (an overestimate). OpenAI prices must be supplied via
  `AGENTOS_PRICING_FILE`. Estimates are not invoices.

## Product gaps

* Development authentication only: seeded users, HS256, tokens in `localStorage`,
  no refresh, revocation or SSO.
* No HTTP fetch tool. Document search is keyword-based; there are no embeddings and
  no reranking.
* The DAG editor is JSON plus a live preview. There is no drag-and-drop.
* Rate limits are keyed by token suffix and path rather than user id; there are no
  per-workspace quotas.
* Events and usage rows are never pruned (no retention job or partitioning).
* Artifacts are stored on a local volume. The S3-compatible backend is not
  implemented.
* The mock planner treats `platform-*.md` documents as the subjects to compare. It is
  a demo policy, not a general planner.
* Old artifact rows created by a non-container run point at files outside the Docker
  volume. Downloading those returns 410 Gone.
