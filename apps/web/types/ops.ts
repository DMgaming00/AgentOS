export type Bucket = { le: number | null; count: number };
export type Dist = { n: number; p50: number | null; p95: number | null; p99: number | null; max: number | null; histogram: Bucket[] };
export type Telemetry = {
  window_days: number; generated_at: string;
  workflow_duration_s: Dist; queue_latency_ms: Dist;
  database_latency: { kind: "probe"; note: string; select_1_ms: { p50: number; p95: number; max: number }; indexed_read_ms: { p50: number; p95: number; max: number }; samples: number };
  model_latency_ms: { overall: Dist; by_model: Record<string, Dist> };
  tool_latency_ms: Record<string, Dist>;
  rates: { attempts: number; retry_rate: number | null; terminal_runs: number; run_failure_rate: number | null; attempt_status: Record<string, number> };
  tokens: { input: number; output: number; calls: number; calls_missing_usage: number };
  estimated_cost: { usd: number; label: string };
  approval_wait_s: Dist & { pending: number; oldest_pending_s: number | null };
  recovery_ms: Dist;
  workers: { live: number; total_seen: number; capacity: number; in_flight: number; utilization: number | null };
  failure_heatmap: { agent: string; error: string; count: number }[];
  dependency_graph: { agent: string; tool: string; calls: number; failed: number }[];
  evaluations: { id: string; agent: string; dataset: string; success: number | null; cases: number | null; completed_at: string | null }[];
};
export type WorkerRow = {
  id: string; hostname: string; pid: number; status: string; concurrency: number; in_flight: number; utilization: number | null; steps_completed: number;
  started_at: string; last_heartbeat_at: string; heartbeat_age_s: number; healthy: boolean;
  recent: { window_minutes: number; attempts: number; succeeded: number; failed: number; mean_queue_wait_ms: number | null };
};
export type WorkersView = { items: WorkerRow[]; summary: { healthy: number; total: number; capacity: number; in_flight: number }; note: string };
export type Inspector = {
  run: { id: string; status: string; workflow: string | null; workflow_version: number; environment: string; purpose: string; trace_id: string | null; created_at: string; started_at: string | null; completed_at: string | null; parent_run_id: string | null };
  correlation: { trace_id: string | null; span_ids_in_events: number; note: string };
  spans: { kind: "run" | "step" | "attempt" | "model_call" | "tool_call" | "approval"; id: string; parent: string | null; label: string; start: string | null; end: string | null; duration_ms: number | null; status: string | null; details: Record<string, unknown> }[];
  tools: { id: string; tool: string; status: string; duration_ms: number | null; requires_approval: boolean; error: string | null; arguments_hash: string; step_id: string }[];
  cost: { label: string; total_usd: number; retry_cost_usd: number; run_reported_usd: number; by_step: { step_id: string | null; node_key: string | null; usd: number }[]; by_model: { model: string; calls: number; cost: number; in: number; out: number }[] };
  versions: { agent: string; agent_id: string; version: number; version_id: string; config_hash: string; source: string; role_now: string; pinned_environment: string; deployments: { id: string; type: string; status: string; canary_percent: number; deployed_at: string; approved_by: string | null }[]; releases: { id: string; environment: string; status: string; role: string }[] }[];
  evaluation: { evaluation_run_id: string; dataset: string; case_key: string; passed: boolean | null; status: string; error: Record<string, unknown> | null; scores: Record<string, { passed: boolean }> }[];
  failure: { signals: { id: string; category: string; detector: string; summary: string; step_id: string | null; infrastructure: boolean }[]; diagnoses: { id: string; agent_id: string; category: string; hypothesis: string; confidence: string; status: string }[] };
  candidates: { id: string; agent_id: string; title: string; status: string; generator: string; rule_id: string | null; candidate_version_id: string | null }[];
  integrity: { events: number; first_sequence: number | null; last_sequence: number | null; gap_free: boolean; monotonic: boolean };
  artifacts: number; links: { flight_recorder: string; live_graph: string };
};
