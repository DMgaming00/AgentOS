/** Chaos Lab payloads (the API returns JSON documents; these mirror agentos_core.chaos). */
export type FaultInfo = { type: string; points: string[]; default_point: string; description: string; expected_platform_response: string };
export type MetricDef = { definition: string; denominator: string; kind: "outcome" | "descriptive" | "latency" | "safety" };
export type Catalog = { faults: FaultInfo[]; limits: { max_runs: number; max_duration_seconds: number; max_safety_budget_usd: number }; metrics: Record<string, MetricDef>; notes: string[] };

export type FaultSpec = { type: string; point?: string | null; probability: number; node_key?: string | null; tool?: string | null; params?: Record<string, number | string>; max_per_step?: number };
export type ChaosPlan = {
  seed: number; faults: FaultSpec[]; trials: number; max_affected_runs: number; duration_seconds: number;
  recovery_policy: "as_configured" | "no_retry" | "extra_retries"; approver: "auto_approve" | "none"; safety_budget_usd: number; run_input?: Record<string, unknown>;
};
export type Scenario = { id: string | null; name: string; description: string; builtin: boolean; plan: ChaosPlan };

export type Rate = { value: number | null; numerator: number; denominator: number };
export type Latency = { n: number; median: number | null; p95: number | null; max: number | null; silent_faults?: number; injections?: number };
export type Scorecard = {
  planned_runs: number; runs_created: number; runs_finished: number; injections_total: number; injections_by_type: Record<string, number>; partial: boolean;
  metrics: {
    task_completion_rate: Rate; fault_exposure: Rate; recovery_rate: Rate;
    retry_amplification: { attempts_per_step: number | null; attempts: number; steps: number; extra_attempts_per_injection: number | null };
    time_to_detection_ms: Latency; time_to_recovery_ms: Latency;
    duplicate_side_effect_prevention: Rate & { duplicate_successful_executions_found: number };
    state_consistency: Rate & { violations: { run_id: string; case_index: number; check: string }[] };
    budget_compliance: Rate; policy_held_under_hostile_output: Rate;
  };
  definitions: Record<string, MetricDef>;
  per_case: { case_index: number; run_id: string; status: string; completed: boolean; injections: number; attempts: number; error: string | null }[];
  scoring_note: string;
};

export type Injection = { id: string; run_id: string | null; case_index: number; fault_type: string; point: string; step_key: string | null; attempt: number | null; call_key: string; detail: Record<string, unknown>; outcome: Record<string, unknown> | null; at: string };
export type ExperimentSummary = {
  id: string; name: string; status: "running" | "completed" | "failed" | "cancelled"; workflow_definition_id: string; created_at: string; started_at: string | null; completed_at: string | null;
  seed: number; trials: number; plan_sha256: string; faults: string[]; conditions: Record<string, unknown>; abort_reason: string | null; completion_rate: number | null; injections_total: number | null; overrides: Record<string, string>;
};
export type ExperimentDetail = ExperimentSummary & { plan: ChaosPlan; results: Scorecard | null; injections: Injection[] };

export type ChaosComparison = {
  baseline_id: string; candidate_id: string;
  same_fault_schedule: { plan_sha256: string; seed: number; trials: number; note: string };
  injections: { baseline: Record<string, number>; candidate: Record<string, number> };
  paired_completion: { n_matched: number; baseline_success: number | null; candidate_success: number | null; diff_mean: number | null; ci95: [number, number] | null; mcnemar_p: number | null; significant: boolean; interpretation: string; regressions: string[]; improvements: string[]; per_case: { case_id: string; baseline: number; candidate: number }[] };
  deltas: Record<string, { baseline: unknown; candidate: unknown; delta?: number | null }>;
  caution: string;
};
