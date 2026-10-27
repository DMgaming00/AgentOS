export type Gate = { gate: string; passed: boolean | null; detail: string; measured: Record<string, unknown> };
export type RuleResult = { rule: string; status: "triggered" | "ok" | "insufficient_evidence" | "disabled"; reason: string; measured: Record<string, unknown> };
export type Evaluation = { stage_index: number; percent: number; since: string; canary_steps: number; baseline_steps: number; rules: RuleResult[]; triggered: string[] };
export type VersionRef = { id: string; version: number; config_hash: string } | null;
export type AgentEnvs = {
  agent_id: string; agent: string;
  environments: {
    production: { label: string; version: VersionRef; canary: VersionRef; canary_percent: number; canary_seed: string | null; live_release?: { id: string; status: string; percent: number } };
    staging: { version: VersionRef; pinned: boolean; falls_back_to: string | null; live_release?: { id: string; status: string; percent: number } };
    development: { version: VersionRef; pinned: boolean; falls_back_to: string | null; live_release?: { id: string; status: string; percent: number } };
  };
};
export type Plan = {
  id: string; agent_id: string; agent: string | null; environment: "development" | "staging" | "production"; status: "pending_approval" | "active" | "completed" | "rolled_back" | "aborted";
  schedule: number[]; stage_index: number; percent: number; seed: string; auto_rollback: boolean; min_stage_samples: number; min_stage_seconds: number;
  candidate_version_id: string; baseline_version_id: string; experiment_id: string | null; request_id: string | null; created_at: string; stage_started_at: string | null; completed_at: string | null; end_reason: string | null; rules: Record<string, Record<string, unknown>>;
};
export type PlanDetail = Plan & {
  baseline_version: number | null; candidate_version: number | null; config_diff: { field: string; before?: unknown; after?: unknown; added?: string[]; removed?: string[] }[];
  approval: { status: string; requested_by: string | null; decided_by: string | null; decided_at: string | null; reason: string | null; expires_at: string } | null;
  evaluation?: Evaluation; stage_gates?: Gate[]; events: { id: number; type: string; actor: string | null; payload: Record<string, unknown>; at: string }[];
};
export type GatePreview = { environment: string; gates: Gate[]; blocking: string[]; can_request: boolean };
