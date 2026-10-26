"use client";

import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";

import type { RunEvent } from "@/types";
import { api, unwrap } from "./api";
import { type StreamState, connectRunStream } from "./sse";

export const qk = {
  runs: (params: object) => ["runs", params] as const,
  run: (id: string) => ["run", id] as const,
  steps: (id: string) => ["steps", id] as const,
  step: (runId: string, stepId: string) => ["step", runId, stepId] as const,
  approvals: (params: object) => ["approvals", params] as const,
  agents: ["agents"] as const,
  agent: (id: string) => ["agent", id] as const,
  workflows: ["workflows"] as const,
  workflow: (id: string) => ["workflow", id] as const,
  metrics: (kind: string, days: number) => ["metrics", kind, days] as const,
};

/** Current time, refreshed on an interval (keeps render pure). */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

export function useRuns(params: { status?: string; limit?: number; offset?: number; workflow_id?: string } = {}) {
  return useQuery({
    queryKey: qk.runs(params),
    queryFn: async () => unwrap(await api.GET("/api/v1/runs", { params: { query: params } })),
    refetchInterval: 5000,
    placeholderData: keepPreviousData,
  });
}

export function useRun(id: string) {
  return useQuery({
    queryKey: qk.run(id),
    queryFn: async () => unwrap(await api.GET("/api/v1/runs/{run_id}", { params: { path: { run_id: id } } })),
  });
}

export function useSteps(id: string) {
  return useQuery({
    queryKey: qk.steps(id),
    queryFn: async () => unwrap(await api.GET("/api/v1/runs/{run_id}/steps", { params: { path: { run_id: id } } })),
  });
}

export function useStepDetail(runId: string, stepId: string | null) {
  return useQuery({
    queryKey: qk.step(runId, stepId ?? ""),
    enabled: !!stepId,
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/runs/{run_id}/steps/{step_id}", { params: { path: { run_id: runId, step_id: stepId! } } })),
  });
}

export function useRunArtifacts(id: string) {
  return useQuery({
    queryKey: ["run-artifacts", id],
    queryFn: async () => unwrap(await api.GET("/api/v1/runs/{run_id}/artifacts", { params: { path: { run_id: id } } })),
  });
}

export function useApprovals(params: { status?: string; run_id?: string; limit?: number } = {}) {
  return useQuery({
    queryKey: qk.approvals(params),
    queryFn: async () => unwrap(await api.GET("/api/v1/approvals", { params: { query: params } })),
    refetchInterval: 5000,
  });
}

export function useAgents() {
  return useQuery({
    queryKey: qk.agents,
    queryFn: async () => unwrap(await api.GET("/api/v1/agents", { params: { query: { limit: 200 } } })),
  });
}

export function useAgent(id: string) {
  return useQuery({
    queryKey: qk.agent(id),
    queryFn: async () => unwrap(await api.GET("/api/v1/agents/{agent_id}", { params: { path: { agent_id: id } } })),
  });
}

export function useAgentExecutions(id: string) {
  return useQuery({
    queryKey: ["agent-executions", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/agents/{agent_id}/executions", { params: { path: { agent_id: id }, query: { limit: 50 } } })),
  });
}

export function useWorkflows() {
  return useQuery({
    queryKey: qk.workflows,
    queryFn: async () => unwrap(await api.GET("/api/v1/workflows", { params: { query: { limit: 200 } } })),
  });
}

export function useWorkflow(id: string) {
  return useQuery({
    queryKey: qk.workflow(id),
    queryFn: async () => unwrap(await api.GET("/api/v1/workflows/{workflow_id}", { params: { path: { workflow_id: id } } })),
  });
}

export function useWorkflowVersions(id: string) {
  return useQuery({
    queryKey: ["workflow-versions", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/workflows/{workflow_id}/versions", { params: { path: { workflow_id: id } } })),
  });
}

export function useTools() {
  return useQuery({ queryKey: ["tools"], queryFn: async () => unwrap(await api.GET("/api/v1/tools")), staleTime: 60_000 });
}

export function useSystem() {
  return useQuery({ queryKey: ["system"], queryFn: async () => unwrap(await api.GET("/api/v1/system")), staleTime: 30_000 });
}

export function useWorkspace() {
  return useQuery({ queryKey: ["workspace"], queryFn: async () => unwrap(await api.GET("/api/v1/workspace")) });
}

export function useDocuments() {
  return useQuery({
    queryKey: ["documents"],
    queryFn: async () => unwrap(await api.GET("/api/v1/documents", { params: { query: { limit: 500 } } })),
  });
}

export function useArtifacts() {
  return useQuery({
    queryKey: ["artifacts"],
    queryFn: async () => unwrap(await api.GET("/api/v1/artifacts", { params: { query: { limit: 200 } } })),
  });
}

type MetricKind = "overview" | "usage" | "cost" | "observability";
const metricPath = {
  overview: "/api/v1/metrics/overview",
  usage: "/api/v1/metrics/usage",
  cost: "/api/v1/metrics/cost",
  observability: "/api/v1/metrics/observability",
} as const;

export function useMetrics<T = Record<string, unknown>>(kind: MetricKind, days = 30, refetchInterval = 10_000) {
  return useQuery({
    queryKey: qk.metrics(kind, days),
    queryFn: async () => unwrap(await api.GET(metricPath[kind], { params: { query: { days } } })) as T,
    refetchInterval,
  });
}

/**
 * Persisted event history + live stream. History is loaded from PostgreSQL first, then the
 * stream resumes after the last sequence; duplicates are dropped by sequence number. Each new
 * event invalidates run/step queries (throttled) so the DAG and panels stay current.
 */
export function useRunEvents(runId: string) {
  const qc = useQueryClient();
  // Keyed by run so switching runs never shows stale events (no reset inside the effect).
  const [store, setStore] = useState<{ runId: string; events: RunEvent[] }>({ runId, events: [] });
  const events = store.runId === runId ? store.events : [];
  const [state, setState] = useState<StreamState>("connecting");
  const seen = useRef<Set<number>>(new Set());
  const lastSeq = useRef(0);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);

  const add = useCallback(
    (incoming: RunEvent[]) => {
      const fresh = incoming.filter((e) => !seen.current.has(e.sequence_number));
      if (!fresh.length) return;
      for (const e of fresh) {
        seen.current.add(e.sequence_number);
        lastSeq.current = Math.max(lastSeq.current, e.sequence_number);
      }
      setStore((prev) => ({
        runId,
        events: [...(prev.runId === runId ? prev.events : []), ...fresh].sort((a, b) => a.sequence_number - b.sequence_number),
      }));
      if (!pending.current) {
        pending.current = setTimeout(() => {
          pending.current = null;
          void qc.invalidateQueries({ queryKey: qk.run(runId) });
          void qc.invalidateQueries({ queryKey: qk.steps(runId) });
          void qc.invalidateQueries({ queryKey: ["step", runId] });
          void qc.invalidateQueries({ queryKey: ["run-artifacts", runId] });
          void qc.invalidateQueries({ queryKey: ["approvals"] });
        }, 250);
      }
    },
    [qc, runId],
  );

  useEffect(() => {
    let cancelled = false;
    let stop: (() => void) | null = null;
    seen.current = new Set();
    lastSeq.current = 0;
    (async () => {
      for (;;) {
        const page = unwrap(
          await api.GET("/api/v1/runs/{run_id}/events", {
            params: { path: { run_id: runId }, query: { after: lastSeq.current, limit: 2000 } },
          }),
        );
        if (cancelled) return;
        add(page);
        if (page.length < 2000) break;
      }
      stop = connectRunStream(runId, {
        lastSequence: () => lastSeq.current,
        onState: (s) => !cancelled && setState(s),
        onMessage: (m) => {
          if (m.event === "stream.end" || !m.data) return;
          try {
            add([JSON.parse(m.data) as RunEvent]);
          } catch {
            /* ignore malformed frame */
          }
        },
      });
    })().catch(() => !cancelled && setState("reconnecting"));
    return () => {
      cancelled = true;
      stop?.();
      if (pending.current) clearTimeout(pending.current);
    };
  }, [runId, add]);

  return { events, state };
}
