/** Flight Recorder payloads (the API returns free-form JSON; these mirror agentos_core.flight.timeline). */
export type FlightFrame = {
  seq: number;
  ts: string;
  elapsed_ms: number | null;
  type: string;
  step_key: string | null;
  severity: "info" | "warning" | "error";
  status_delta: Record<string, string>;
  trace_id: string | null;
  span_id: string | null;
  payload: unknown;
};

export type FlightStep = {
  key: string;
  type: string;
  status: string;
  agent_version_id: string | null;
  depends_on: string[];
  attempts: number;
  failures: number;
  fan_out_parent: string | null;
  when: unknown;
  started_ms: number | null;
  completed_ms: number | null;
  duration_ms: number | null;
  output: unknown;
  error: unknown;
};

export type FlightModelCall = {
  id: string;
  step_key: string;
  call_index: number;
  attempt: number | null;
  provider: string;
  model: string;
  agent_version_id: string | null;
  status: string;
  latency_ms: number;
  request_digest: string;
  truncated: boolean;
  at_ms: number | null;
  finish_reason: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  tool_calls: string[];
  error_type: string | null;
};

export type FlightToolExecution = {
  id: string;
  step_key: string | null;
  tool: string;
  status: string;
  arguments_hash: string;
  arguments: unknown;
  result: unknown;
  error: unknown;
  requires_approval: boolean;
  reason: string | null;
  duration_ms: number | null;
  at_ms: number | null;
};

export type FlightAttempt = {
  step_key: string | null;
  attempt: number;
  worker_id: string;
  status: string;
  started_ms: number | null;
  finished_ms: number | null;
  queue_wait_ms: number | null;
  error_type: string | null;
};

export type FlightApproval = {
  id: string;
  step_key: string | null;
  action: string;
  status: string;
  requested_ms: number | null;
  decided_ms: number | null;
  decision_reason: string | null;
};

export type FlightRecording = {
  schema_version: number;
  mode: "recorded";
  run: {
    id: string;
    workflow: string | null;
    workflow_version: number;
    status: string;
    purpose: string;
    replay_of_run_id: string | null;
    parent_run_id: string | null;
    priority: number;
    trace_id: string | null;
    created_at: string;
    completed_at: string | null;
    duration_ms: number | null;
    cost_spent_usd: string;
    cost_basis_note: string;
    model_calls: number;
    tool_calls: number;
    input: unknown;
    output: unknown;
    error: unknown;
  };
  steps: FlightStep[];
  attempts: FlightAttempt[];
  model_calls: FlightModelCall[];
  tool_executions: FlightToolExecution[];
  approvals: FlightApproval[];
  initial_status: Record<string, string>;
  frames: FlightFrame[];
  integrity: { event_count: number; first_seq: number | null; last_seq: number | null; contiguous: boolean; gaps: number[][]; sha256: string };
};

export type ModelCallDetail = {
  id: string;
  step_key: string;
  provider: string;
  model: string;
  status: string;
  request_digest: string;
  truncated: boolean;
  request: { system?: string; messages?: { role: string; text?: string; tool_calls?: unknown[]; tool_results?: unknown[] }[]; tools?: unknown[]; config?: unknown; meta?: unknown };
  response: { text?: string; tool_calls?: unknown[]; finish_reason?: string; usage?: unknown } | null;
  error: unknown;
};

export type StepDiff = {
  key: string;
  presence: "both" | "only_a" | "only_b";
  status?: [string, string];
  attempts?: [number, number];
  duration_ms?: [number | null, number | null];
  differences: { kind: string; [k: string]: unknown }[];
  model_calls?: { index: number; request_same: boolean; response_same: boolean; a: { text: string | null; tools: string[]; latency_ms: number } | null; b: { text: string | null; tools: string[]; latency_ms: number } | null }[];
  tool_calls?: { a: string[]; b: string[] };
};

export type RunDiff = {
  a: string;
  b: string;
  summary: {
    steps_total: number;
    steps_different: number;
    branch_differences: string[];
    retry_differences: string[];
    final_status: [string, string];
    final_output_same: boolean;
    final_output_diff: { path: string; a: string | null; b: string | null; kind: string }[];
    duration_ms: [number | null, number | null];
    cost_usd_estimated_simulated: [string, string];
    model_calls: [number, number];
    tool_calls: [number, number];
    identical: boolean;
    evaluation?: { passed: [boolean | null, boolean | null]; same_verdict: boolean };
  };
  steps: StepDiff[];
};
