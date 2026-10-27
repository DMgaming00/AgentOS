export type Budget = {
  id: string; scope: "workspace" | "agent"; agent_id: string | null; agent?: string | null; period: "daily" | "monthly"; mode: "enforce" | "alert"; enabled: boolean;
  limit_usd: number; warn_fraction: number; period_start: string; period_end: string; spent_usd: number; reserved_usd: number; utilization: number;
  state: "ok" | "warning" | "exceeded"; projected_period_spend_usd: number | null; projection_note: string;
};
export type AgentCost = { agent_id: string | null; agent: string; calls: number; estimated_cost_usd: number | null; share: number | null; successful_steps: number; failed_steps: number; cost_per_successful_step_usd: number | null; retry_cost_usd: number | null; failed_run_cost_usd: number | null };
export type FinopsSummary = {
  label: string; window_days: number; truncated: boolean; notes: string[];
  totals: { calls: number; estimated_cost_usd: number | null; calls_with_unknown_cost: number; simulated_share: number | null; retry_cost_usd: number | null; retry_cost_share: number | null; failed_run_cost_usd: number | null; failed_run_cost_share: number | null; pricing_versions: string[] };
  by_agent: AgentCost[];
  by_workflow: { workflow: string; runs: number; succeeded_runs: number; estimated_cost_usd: number | null; cost_per_succeeded_run_usd: number | null; failed_run_cost_usd: number | null }[];
  by_model: { provider: string; model: string; calls: number; input_tokens: number; output_tokens: number; estimated_cost_usd: number | null; latency_p50_ms: number | null; latency_p95_ms: number | null; cost_bases: string[] }[];
  by_purpose: Record<string, number>;
  tools: { tool: string; calls: number; failed: number; denied: number }[];
  daily: { day: string; calls: number; estimated_cost_usd: number }[];
  trend: { last_7_days_usd: number; previous_7_days_usd: number; change: number | null } | null;
  budgets: Budget[]; pricing: { version: string; label: string };
};
export type Anomaly = { id: string; kind: string; severity: "info" | "warning" | "high"; subject: string; summary: string; evidence: Record<string, unknown>; status: "open" | "acknowledged"; detected_at: string };
export type Suggestion = { id: string; title: string; evidence: Record<string, unknown>; action: string; savings: { basis: "measured" | "projected" | "none"; usd: number | null; note: string } };
