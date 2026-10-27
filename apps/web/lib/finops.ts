"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { Anomaly, Budget, FinopsSummary, Suggestion } from "@/types/finops";
import { api, idempotencyKey, unwrap } from "./api";

export const useFinops = (days: number) =>
  useQuery({ queryKey: ["finops", "summary", days], queryFn: async () => unwrap(await api.GET("/api/v1/finops/summary", { params: { query: { days } } })) as unknown as FinopsSummary, refetchInterval: 15_000 });

export const useBudgets = () =>
  useQuery({ queryKey: ["finops", "budgets"], queryFn: async () => (unwrap(await api.GET("/api/v1/finops/budgets")) as unknown as { items: Budget[] }).items, refetchInterval: 10_000 });

export const useAnomalies = () =>
  useQuery({ queryKey: ["finops", "anomalies"], queryFn: async () => unwrap(await api.GET("/api/v1/finops/anomalies", { params: { query: {} } })) as unknown as { items: Anomaly[]; thresholds: Record<string, number> }, refetchInterval: 15_000 });

export const useSuggestions = (days: number) =>
  useQuery({ queryKey: ["finops", "suggestions", days], queryFn: async () => unwrap(await api.GET("/api/v1/finops/suggestions", { params: { query: { days } } })) as unknown as { items: Suggestion[]; note: string } });

export function useFinopsMutations() {
  const qc = useQueryClient();
  const done = () => void qc.invalidateQueries({ queryKey: ["finops"] });
  return {
    createBudget: useMutation({
      mutationFn: async (b: { scope: "workspace" | "agent"; period: "daily" | "monthly"; limit_usd: string; mode: "enforce" | "alert"; agent_id?: string }) =>
        unwrap(await api.POST("/api/v1/finops/budgets", { body: { ...b, agent_id: b.agent_id ?? null, warn_fraction: "0.8" } as never, params: { header: { "Idempotency-Key": idempotencyKey() } } })),
      onSuccess: done,
    }),
    patchBudget: useMutation({
      mutationFn: async (v: { id: string; body: { limit_usd?: string; mode?: "enforce" | "alert"; enabled?: boolean } }) =>
        unwrap(await api.PATCH("/api/v1/finops/budgets/{budget_id}", { params: { path: { budget_id: v.id } }, body: v.body as never })),
      onSuccess: done,
    }),
    deleteBudget: useMutation({
      mutationFn: async (id: string) => unwrap(await api.DELETE("/api/v1/finops/budgets/{budget_id}", { params: { path: { budget_id: id } } })),
      onSuccess: done,
    }),
    detect: useMutation({ mutationFn: async () => unwrap(await api.POST("/api/v1/finops/anomalies/detect")) as unknown as { found: number; new: number }, onSuccess: done }),
    acknowledge: useMutation({
      mutationFn: async (id: string) => unwrap(await api.POST("/api/v1/finops/anomalies/{anomaly_id}/acknowledge", { params: { path: { anomaly_id: id } } })),
      onSuccess: done,
    }),
  };
}
