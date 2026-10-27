export type Family = "extraction" | "calculation" | "grounded_qa";
export type Split = "development" | "validation" | "test";
export type Stage = "selection_running" | "selection_complete" | "final_test_running" | "completed" | "failed";
export type CorpusDataset = { name: string; family: Family; split: Split; version: number; cases: number; manifest_hash: string; stored_hash: string | null; state: "verified" | "missing" | "mismatch"; kinds: Record<string, number> };
export type Corpus = {
  corpus: string; version: number; total_cases: number; split_salt: number; all_verified: boolean; datasets: CorpusDataset[];
  leakage: { identical_case_across_splits: number; identical_fact_set_across_splits: Record<string, number>; facts_shared_between_splits: Record<string, number>; distinct_facts_in_documents: number; note: string };
  source_documents: { filename: string; sha256: string; present_in_workspace: boolean }[];
};
export type Contender = { agent_id: string; name: string; description: string; provider: string; model: string; tools: string[]; max_steps: number; agent_version_id: string | null; eligible: boolean; arena_contender: boolean };
export type FamilyScore = { n: number; passed: number; rate: number | null; ci95: [number, number] | null };
export type LeaderRow = {
  agent_id: string; agent: string; version: number; agent_version_id: string; config_hash: string; rank: number;
  overall: { score: number | null; ci95: [number, number] | null }; families: Record<Family, FamilyScore>; cases: number; passed: number;
  cost_usd: number | null; cost_per_success_usd: number | null; latency_ms: { p50: number | null; p95: number | null; measured: number };
  tool_calls: number; failure_breakdown: Record<string, number>; incomplete_executions: number;
};
export type Leaderboard = { rows: LeaderRow[]; adjacent_comparisons: { higher: string; lower: string; difference: number; ci95: [number, number] | null; separated: boolean }[]; method: Record<string, string> };
export type SelectionVsTest = { agent: string; agent_version_id: string; validation_score: number | null; test_score: number | null; gap: number | null; validation_rank: number; test_rank: number; gap_flag: boolean };
export type ArenaSummary = {
  id: string; name: string; created_at: string; stage: Stage; final_test_started_at: string | null; progress: Record<string, Record<string, number>>;
  config: { contenders: { agent: string; version: number; version_id: string; config_hash: string }[]; dataset_hashes: Record<string, string>; scoring: string; offline_only: boolean };
};
export type ArenaDetail = ArenaSummary & { leaderboards: Partial<Record<Split, Leaderboard>>; selection_vs_test: SelectionVsTest[] | null };
export type CaseResult = { agent_version_id: string; family: Family; split: Split; case_key: string; kind: string | null; task: string | null; passed: boolean | null; category: string | null; error_type: string | null; expected: unknown; observed: unknown; workflow_run_id: string | null };
