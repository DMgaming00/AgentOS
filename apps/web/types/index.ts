import type { components } from "./api";

type S = components["schemas"];
export type Run = S["RunOut"];
export type Step = S["StepOut"];
export type StepDetail = S["StepDetail"];
export type Attempt = S["AttemptOut"];
export type RunEvent = S["EventOut"];
export type Approval = S["ApprovalOut"];
export type Agent = S["AgentOut"];
export type AgentCreate = S["AgentCreate"];
export type AgentUpdate = S["AgentUpdate"];
export type Workflow = S["WorkflowOut"];
export type WorkflowSummary = S["WorkflowSummary"];
export type WorkflowDetail = S["WorkflowDetail"];
export type ValidateResponse = S["ValidateResponse"];
export type PlanResponse = S["PlanResponse"];
export type Artifact = S["ArtifactOut"];
export type DocumentOut = S["DocumentOut"];
export type Tool = S["ToolOut"];
export type ToolExecution = S["ToolExecutionOut"];
export type ModelUsage = S["ModelUsageOut"];
export type Membership = S["Membership"];
export type User = S["UserOut"];
export type SystemInfo = S["SystemInfo"];
export type WorkspaceInfo = S["WorkspaceOut"];

export type RunStatus = "pending" | "planning" | "running" | "waiting_approval" | "succeeded" | "failed" | "cancelled";
export type StepStatus =
  | "pending" | "ready" | "running" | "waiting_approval" | "retry_wait" | "succeeded" | "failed" | "skipped" | "cancelled";
