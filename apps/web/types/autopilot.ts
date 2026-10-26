import type { components } from "./api";

type S = components["schemas"];
export type AgentVersion = S["AgentVersionOut"];
export type Signal = S["SignalOut"];
export type Diagnosis = S["DiagnosisOut"];
export type DiagnosisDetail = S["DiagnosisDetail"];
export type Failures = S["FailuresOut"];
export type Candidate = S["CandidateOut"];
export type Dataset = S["DatasetOut"];
export type DatasetDetail = S["DatasetDetail"];
export type EvaluationRun = S["EvaluationRunOut"];
export type Experiment = S["ExperimentOut"];
export type DeploymentRequest = S["DeploymentRequestOut"];
export type Deployment = S["DeploymentOut"];
export type AutopilotEvent = S["AutopilotEventOut"];
export type Policy = S["AutopilotPolicy"];

export type Rate = { value: number | null; numerator: number; denominator: number };
export type EvalMetrics = {
  attempts: number;
  cases: number;
  task_success_rate: Rate;
  tool_call_correctness: Rate;
  schema_compliance: Rate;
  grounding_pass_rate: Rate;
  numeric_accuracy: Rate;
  failure_rate: Rate;
  retry_rate: Rate;
  total_cost_usd: number | null;
  cost_per_successful_task_usd: number | null;
  latency_ms: { mean: number | null; p50: number | null; p95: number | null; measured: number };
  failure_breakdown: Record<string, number>;
  incomplete_executions?: number;
  planned_executions?: number;
};
export type Gate = { gate: string; passed: boolean; severity: "hard" | "soft"; detail: string; measured: Record<string, unknown> };
