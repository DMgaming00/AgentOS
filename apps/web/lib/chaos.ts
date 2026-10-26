"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { Catalog, ChaosComparison, ChaosPlan, ExperimentDetail, ExperimentSummary, Scenario } from "@/types/chaos";
import { api, idempotencyKey, unwrap } from "./api";

export const useChaosCatalog = () =>
  useQuery({ queryKey: ["chaos", "catalog"], queryFn: async () => unwrap(await api.GET("/api/v1/chaos/catalog")) as unknown as Catalog, staleTime: 300_000 });

export const useScenarios = () =>
  useQuery({ queryKey: ["chaos", "scenarios"], queryFn: async () => unwrap(await api.GET("/api/v1/chaos/scenarios")) as unknown as Scenario[] });

export const useExperiments = () =>
  useQuery({
    queryKey: ["chaos", "experiments"],
    queryFn: async () => unwrap(await api.GET("/api/v1/chaos/experiments", { params: { query: { limit: 100 } } })) as unknown as { items: ExperimentSummary[] },
    refetchInterval: 3000,
  });

export function useExperiment(id: string) {
  return useQuery({
    queryKey: ["chaos", "experiment", id],
    queryFn: async () =>
      unwrap(await api.GET("/api/v1/chaos/experiments/{experiment_id}", { params: { path: { experiment_id: id } } })) as unknown as ExperimentDetail,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 1500 : false),
  });
}

export function useChaosCompare(a: string | null, b: string | null) {
  return useQuery({
    queryKey: ["chaos", "compare", a, b],
    enabled: !!a && !!b,
    retry: false,
    queryFn: async () => unwrap(await api.POST("/api/v1/chaos/compare", { body: { baseline_id: a!, candidate_id: b! } })) as unknown as ChaosComparison,
  });
}

export type StartBody = { name: string; workflow_id: string; plan?: ChaosPlan; scenario?: string; overrides?: Record<string, unknown>; candidate_id?: string | null };

export function useStartExperiment() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (body: StartBody) =>
      unwrap(
        await api.POST("/api/v1/chaos/experiments", { body: body as never, params: { header: { "Idempotency-Key": idempotencyKey() } } }),
      ) as unknown as ExperimentSummary,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["chaos"] }),
  });
}

export function useAbortExperiment(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/chaos/experiments/{experiment_id}/abort", { params: { path: { experiment_id: id } } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["chaos"] }),
  });
}
