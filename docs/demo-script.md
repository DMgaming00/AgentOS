# Demo script (about 8 minutes)

Setup: `make up`, then open http://localhost:3000 and sign in as
`admin@agentos.dev` / `agentos-admin`. Everything runs on the deterministic mock
provider, and the banner at the top says so.

## 1. Multi-agent research report (Demo A), ~3 min

1. **Workflows -> research-report.** Show the DAG: three research agents in
   parallel, then analysis, then the writer. Point out that the definition is
   versioned and validated live against agent tool permissions.
2. **Start run.** On the run page, the graph animates as steps start. The Live badge
   is the SSE stream, and the step timeline shows the three research attempts
   overlapping.
3. Click **research_helix -> Tool calls.** It shows the document_search query and
   ranked excerpts with citations (`platform-helix.md#Helix Serve > Pricing`).
   **Model calls** shows token counts, latency and the simulated cost basis.
4. The run stops at **Waiting approval.** The card shows the agent, tool, affected
   resource, expiry, arguments hash, and the full Markdown it wants to write.
   Nothing has been written yet (Artifacts: none).
5. **Approve.** The run completes. Preview and download the report, and show the
   conflict section: Helix's uptime SLA is 99.9% in one section and 99.95% in
   another. Show the missing-evidence section: Helix publishes no cold-start figure.
6. **Refresh the page.** Same state, same events, because everything is persisted.

CLI equivalent: `make demo-research`.

## 2. Worker failure recovery (Demo B), ~2 min

```bash
make demo-recovery       # or start "failure-recovery" from the UI and watch
docker compose ps worker # one worker container restarted (Up a few seconds)
```

`process_a` deliberately kills its worker process 1 s into attempt 1 (exit 137, no
cleanup). On the run page:

* The timeline shows attempt 1 as `lease expired` on worker X, and attempt 2 on
  worker Y after about one lease TTL (15 s by default).
* `fetch_input` and `process_b` show one attempt each: completed work was not re-run.
* Approve the export; the run succeeds.

## 3. Permission enforcement (Demo C), ~2 min

Start **approval-workflow** (or `make demo-permissions`).

1. **Approvals:** reject `approved-note.md` with a note. Artifacts stays empty; the
   rejected action never ran.
2. The writer resubmits, as a new request with its own approval. Approve it.
3. Its worker is killed right after the write, before recording the step. The retry
   finds the succeeded tool execution by idempotency key and replays it. There is
   still exactly one artifact, and the step output shows `"idempotent_replay": true`.
4. In the same run, `untrusted_attempt` asked for `artifact_writer`, which it is not
   allowed to use. Its Tool calls tab shows `denied`, and the Observability tool
   activity counts it.

## 4. Observability, ~1 min

The **Observability** page shows latency, cost by agent, failures by type
(`worker_lost`, `approval_rejected`), tool activity, worker heartbeats, and recent
errors with trace ids. Every figure is computed from persisted rows.
