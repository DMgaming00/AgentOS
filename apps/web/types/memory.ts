export type MemoryRecord = {
  id: string; namespace: string; kind: "kv" | "note" | "task_summary"; key: string | null; content: string; tags: string[]; version: number;
  expires_at: string | null; deleted_at: string | null; state: "live" | "deleted" | "expired"; provenance: Record<string, unknown>; created_at: string; updated_at: string;
};
export type MemoryHit = { id: string; namespace: string; kind: string; key: string | null; content: string; tags: string[]; version: number; score: number; provenance: Record<string, unknown>; untrusted: true };
export type MemoryVersion = { version: number; change_type: string; content: string; tags: string[]; changed_by: string | null; changed_at: string };
export type AuditEvent = { id: string; action: string; actor_type: string; actor_id: string | null; record_id: string | null; details: Record<string, unknown>; at: string };
export type Rate = { value: number | null; numerator: number; denominator: number; note?: string };
export type StrategyResult = {
  accuracy: Rate; errors: Record<string, { count: number; rate: number | null }>; stale_memory_errors: { count: number; denominator: number };
  incorrect_memory_reuse: { count: number; denominator: number }; by_kind: Record<string, { n: number; correct: number; rate: number }>;
  retrieval_precision: Rate; answer_recall: Rate; context: { mean_chars: number; mean_tokens_est: number; max_chars: number; snippets_mean: number };
  latency_ms: { p50: number | null; p95: number | null; measured: number }; cost_usd_simulated: number | null; evaluation_run_id: string;
};
export type Comparison = { candidate: string; baseline: string; n_matched: number; baseline_success: number | null; candidate_success: number | null; diff_mean: number | null; ci95: [number, number] | null; mcnemar_p: number | null; significant: boolean };
export type MemoryExperiment = {
  id: string; status: "running" | "completed" | "failed" | "cancelled"; created_at: string; completed_at: string | null; error: string | null; strategies: string[];
  definitions?: Record<string, string>; results?: { strategies: Record<string, StrategyResult>; comparisons: Comparison[]; tasks: number; facts_stored: number; caveats: string[] } | null;
};
