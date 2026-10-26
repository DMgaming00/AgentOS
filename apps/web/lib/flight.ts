"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { FlightRecording, ModelCallDetail, RunDiff } from "@/types/flight";
import { api, idempotencyKey, rawFetch, unwrap } from "./api";

export function useFlight(runId: string) {
  return useQuery({
    queryKey: ["flight", runId],
    queryFn: async () => unwrap(await api.GET("/api/v1/runs/{run_id}/flight", { params: { path: { run_id: runId } } })) as unknown as FlightRecording,
    staleTime: 15_000,
  });
}

export function useModelCall(runId: string, recordId: string | null) {
  return useQuery({
    queryKey: ["flight", runId, "call", recordId],
    enabled: !!recordId,
    queryFn: async () =>
      unwrap(
        await api.GET("/api/v1/runs/{run_id}/flight/model-calls/{record_id}", { params: { path: { run_id: runId, record_id: recordId! } } }),
      ) as unknown as ModelCallDetail,
  });
}

export function useRunDiff(a: string | null, b: string | null) {
  return useQuery({
    queryKey: ["flight", "compare", a, b],
    enabled: !!a && !!b,
    queryFn: async () => unwrap(await api.GET("/api/v1/flight/compare", { params: { query: { a: a!, b: b! } } })) as unknown as RunDiff,
  });
}

export function useFlightRuns(purpose: "workflow" | "replay") {
  return useQuery({
    queryKey: ["flight", "runs", purpose],
    queryFn: async () => unwrap(await api.GET("/api/v1/runs", { params: { query: { purpose, limit: 100 } } })),
    refetchInterval: 8000,
  });
}

/** Deterministic re-execution: creates a NEW replay run; the recording itself is never touched. */
export function useReexecute(runId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () =>
      unwrap(
        await api.POST("/api/v1/runs/{run_id}/reexecute", {
          params: { path: { run_id: runId }, header: { "Idempotency-Key": idempotencyKey() } },
        }),
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["flight", "runs"] }),
  });
}

export async function exportFlight(runId: string) {
  const res = await rawFetch(`/api/v1/runs/${runId}/export`);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `agentos-flight-${runId.slice(0, 8)}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
