export type Rate = { value: number | null; numerator: number; denominator: number };
export type Counts = { total: number; pass: number; fail: number; error: number };
export type SecurityResults = {
  suite_version: string;
  counts: { policy: Counts; injection: Counts; benign: Counts; exposure: Counts };
  metrics: {
    policy_enforcement: Rate; attacks_exercised: Rate; unauthorized_action_attempts: number; blocked_actions: number; actions_executed_within_policy: number;
    successful_policy_enforcement: Rate; unsafe_side_effects: number; unsafe_side_effects_prevented: { writes_attempted: number; writes_denied: number };
    false_positive_blocks: Rate; approval_bypass_attempts: { attempts: number; cases_failed: number };
    cross_workspace_access_attempts: { cases: number; canary_leaks: number }; secret_exposure_in_traces: Rate;
  };
  definitions: Record<string, string>;
  matrix: Record<string, Record<string, "pass" | "fail" | "error">>;
  findings: { case_key: string; kind: string; verdict: string; category: string; observed: Record<string, unknown>; mitigation: string }[];
  limitations: string[];
};
export type RunSummary = { id: string; suite_version: string; status: string; phase: string; created_at: string; completed_at: string | null; counts: SecurityResults["counts"] | null; error: string | null };
export type Incident = { id: string; at: string; source: string; severity: "info" | "warning" | "high"; title: string; detail: string | null; run_id: string | null; step_key: string | null; origin: string; evidence: Record<string, unknown> };
export type Overview = { suite_version: string; latest: (RunSummary & { results: SecurityResults }) | null; running: RunSummary | null; incidents_30d: Record<string, number>; incident_total_30d: number; recent_incidents: Incident[] };
export type CaseRow = { case_key: string; kind: string; category: string; title: string; expected: string | null; verdict: "pass" | "fail" | "error"; observed: Record<string, unknown> | null; evidence: Record<string, unknown> | null; workflow_run_id: string | null };
export type RunDetail = RunSummary & { results: SecurityResults | null; cases: CaseRow[]; conditions: Record<string, unknown> };
