"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { Incident, Overview, RunDetail } from "@/types/security";
import { api, idempotencyKey, unwrap } from "./api";

export const useSecurityOverview = () =>
  useQuery({ queryKey: ["security", "overview"], queryFn: async () => unwrap(await api.GET("/api/v1/security/overview")) as unknown as Overview, refetchInterval: (q) => (q.state.data?.running ? 2000 : 15_000) });

export const useSecurityRun = (id: string | null) =>
  useQuery({
    queryKey: ["security", "run", id],
    enabled: !!id,
    queryFn: async () => unwrap(await api.GET("/api/v1/security/runs/{run_id}", { params: { path: { run_id: id! } } })) as unknown as RunDetail,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 2000 : false),
  });

export const useIncidents = (source?: string) =>
  useQuery({
    queryKey: ["security", "incidents", source],
    queryFn: async () => unwrap(await api.GET("/api/v1/security/incidents", { params: { query: { source: source || undefined, days: 30 } } })) as unknown as { items: Incident[]; total: number },
    refetchInterval: 15_000,
  });

export function useStartSecurityRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (agent: "mock" | "live" = "mock") =>
      unwrap(await api.POST("/api/v1/security/runs", { params: { header: { "Idempotency-Key": idempotencyKey() }, query: { agent } } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["security"] }),
  });
}
