"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import type { AutopilotEvent } from "@/types/autopilot";
import { api, unwrap } from "./api";
import { type StreamState, connectEventStream } from "./sse";

export function useAutopilotOverview() {
  return useQuery({
    queryKey: ["autopilot", "overview"],
    queryFn: async () => unwrap(await api.GET("/api/v1/autopilot/overview")) as Record<string, unknown>,
    refetchInterval: 5000,
  });
}

export function useFailures(windowHours = 168) {
  return useQuery({
    queryKey: ["autopilot", "failures", windowHours],
    queryFn: async () => unwrap(await api.GET("/api/v1/autopilot/failures", { params: { query: { window_hours: windowHours } } })),
    refetchInterval: 10_000,
  });
}

export function useDiagnosis(id: string | null) {
  return useQuery({
    queryKey: ["autopilot", "diagnosis", id],
    enabled: !!id,
    queryFn: async () => unwrap(await api.GET("/api/v1/autopilot/failures/{diagnosis_id}", { params: { path: { diagnosis_id: id! } } })),
  });
}

export function useCandidates(agentId?: string) {
  return useQuery({
    queryKey: ["autopilot", "candidates", agentId],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/autopilot/candidates", { params: { query: { agent_id: agentId, limit: 200 } } })),
    refetchInterval: 5000,
  });
}

export function useVersions(agentId: string | null) {
  return useQuery({
    queryKey: ["autopilot", "versions", agentId],
    enabled: !!agentId,
    queryFn: async () => unwrap(await api.GET("/api/v1/agents/{agent_id}/versions", { params: { path: { agent_id: agentId! } } })),
  });
}

export function useDatasets() {
  return useQuery({
    queryKey: ["autopilot", "datasets"],
    queryFn: async () => unwrap(await api.GET("/api/v1/evaluations/datasets", { params: { query: { limit: 200 } } })),
  });
}

export function useDataset(id: string | null) {
  return useQuery({
    queryKey: ["autopilot", "dataset", id],
    enabled: !!id,
    queryFn: async () => unwrap(await api.GET("/api/v1/evaluations/datasets/{dataset_id}", { params: { path: { dataset_id: id! } } })),
  });
}

export function useEvaluationRuns() {
  return useQuery({
    queryKey: ["autopilot", "eval-runs"],
    queryFn: async () => unwrap(await api.GET("/api/v1/evaluations/runs", { params: { query: { limit: 100 } } })),
    refetchInterval: 3000,
  });
}

export function useEvaluationResults(id: string | null) {
  return useQuery({
    queryKey: ["autopilot", "eval-results", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/evaluations/runs/{run_id}/results", { params: { path: { run_id: id! } } })) as {
        status: string;
        dataset: Record<string, unknown>;
        conditions: Record<string, unknown>;
        metrics: Record<string, unknown> | null;
        results: { case_key: string; attempt: number; passed: boolean | null; scores: Record<string, { passed: boolean; details: { failures?: string[] } }> | null; usage: Record<string, number | null> | null; error: Record<string, string> | null; workflow_run_id: string | null }[];
      },
    refetchInterval: 3000,
  });
}

export function useExperiments() {
  return useQuery({
    queryKey: ["autopilot", "experiments"],
    queryFn: async () => unwrap(await api.GET("/api/v1/experiments", { params: { query: { limit: 100 } } })),
    refetchInterval: 3000,
  });
}

export function useExperiment(id: string) {
  return useQuery({
    queryKey: ["autopilot", "experiment", id],
    queryFn: async () => unwrap(await api.GET("/api/v1/experiments/{experiment_id}", { params: { path: { experiment_id: id } } })),
    refetchInterval: (q) => (q.state.data?.status === "running" ? 2000 : false),
  });
}

export function useDeploymentRequests(status?: string) {
  return useQuery({
    queryKey: ["autopilot", "deploy-requests", status],
    queryFn: async () => unwrap(await api.GET("/api/v1/deployments/requests", { params: { query: { status, limit: 100 } } })),
    refetchInterval: 5000,
  });
}

export function useDeploymentHistory(agentId?: string) {
  return useQuery({
    queryKey: ["autopilot", "deploy-history", agentId],
    queryFn: async () => unwrap(await api.GET("/api/v1/deployments/history", { params: { query: { agent_id: agentId, limit: 100 } } })),
    refetchInterval: 5000,
  });
}

export function useDeploymentHealth(id: string | null) {
  return useQuery({
    queryKey: ["autopilot", "deploy-health", id],
    enabled: !!id,
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/deployments/{deployment_id}/health", { params: { path: { deployment_id: id! } } })) as {
        current: { n: number; failed: number; failure_rate: number | null };
        previous: { n: number; failed: number; failure_rate: number | null } | null;
        regression: boolean;
        note: string | null;
      },
    refetchInterval: 5000,
  });
}

export function usePolicy() {
  return useQuery({ queryKey: ["autopilot", "policy"], queryFn: async () => unwrap(await api.GET("/api/v1/autopilot/policy")) });
}

/** Persisted Autopilot events + live SSE; every new event refreshes Autopilot queries. */
export function useAutopilotEvents() {
  const qc = useQueryClient();
  const [events, setEvents] = useState<AutopilotEvent[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const last = useRef(0);
  useEffect(() => {
    let stop: (() => void) | null = null;
    let cancelled = false;
    (async () => {
      const initial = unwrap(await api.GET("/api/v1/autopilot/events", { params: { query: { after: 0, limit: 1000 } } }));
      if (cancelled) return;
      last.current = initial.at(-1)?.sequence_number ?? 0;
      setEvents(initial);
      stop = connectEventStream("/api/v1/autopilot/stream", {
        lastSequence: () => last.current,
        onState: (s) => !cancelled && setState(s),
        onMessage: (m) => {
          if (!m.data) return;
          try {
            const e = JSON.parse(m.data) as AutopilotEvent;
            if (e.sequence_number <= last.current) return;
            last.current = e.sequence_number;
            setEvents((prev) => [...prev, e]);
            void qc.invalidateQueries({ queryKey: ["autopilot"] });
          } catch {
            /* ignore malformed frame */
          }
        },
      });
    })().catch(() => !cancelled && setState("reconnecting"));
    return () => {
      cancelled = true;
      stop?.();
    };
  }, [qc]);
  return { events, state };
}

export function pct(v: number | null | undefined, digits = 1): string {
  return v == null ? "n/a" : `${(v * 100).toFixed(digits)}%`;
}

export function rate(r: { value: number | null; numerator: number; denominator: number } | undefined | null): string {
  if (!r) return "n/a";
  if (r.value == null) return `n/a (0/${r.denominator})`;
  return `${(r.value * 100).toFixed(1)}% (${r.numerator}/${r.denominator})`;
}
