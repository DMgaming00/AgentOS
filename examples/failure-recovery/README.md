# Demo B: worker failure recovery

`failure-recovery` workflow: `fetch_input` (structured_data_reader on
`benchmarks.csv`) -> `process_a` + `process_b` -> `aggregate` -> `export`
(artifact_writer, approval-gated).

`process_a` carries deterministic fault injection:

```json
"inject": {"sleep_seconds": 2, "crash_worker_on_attempt": 1, "crash_after_seconds": 1}
```

The worker executing attempt 1 calls `os._exit(137)`, with no cleanup and no lease
release. The lease expires after `AGENTOS_LEASE_TTL_SECONDS`, the coordinator records
the attempt as `lease_expired` (`worker_lost`), and another worker retries the step.
Completed steps are not re-run. No manual database changes are involved.

Run: `make demo-recovery` (needs two workers or a restart policy; docker compose
provides both). Fault injection is refused in production
(`AGENTOS_ALLOW_FAULT_INJECTION` must be false).
