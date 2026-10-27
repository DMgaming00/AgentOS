"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { AgentEnvs, GatePreview, Plan, PlanDetail } from "@/types/release";
import { api, idempotencyKey, unwrap } from "./api";

export const useEnvironments = () =>
  useQuery({ queryKey: ["release", "environments"], queryFn: async () => unwrap(await api.GET("/api/v1/releases/environments", { params: { query: {} } })) as unknown as { items: AgentEnvs[]; note: string }, refetchInterval: 5000 });

export const useReleases = () =>
  useQuery({ queryKey: ["release", "list"], queryFn: async () => (unwrap(await api.GET("/api/v1/releases", { params: { query: {} } })) as unknown as { items: Plan[] }).items, refetchInterval: 4000 });

export const useRelease = (id: string | null) =>
  useQuery({ queryKey: ["release", "detail", id], enabled: !!id, queryFn: async () => unwrap(await api.GET("/api/v1/releases/{plan_id}", { params: { path: { plan_id: id! } } })) as unknown as PlanDetail, refetchInterval: 3000 });

export const useGatePreview = (agentId: string | null, experimentId: string | null, environment: "staging" | "production") =>
  useQuery({
    queryKey: ["release", "gates", agentId, experimentId, environment],
    enabled: !!agentId,
    queryFn: async () => unwrap(await api.GET("/api/v1/releases/gates", { params: { query: { agent_id: agentId!, experiment_id: experimentId || undefined, environment } } })) as unknown as GatePreview,
  });

export function useReleaseMutations() {
  const qc = useQueryClient();
  const done = () => void qc.invalidateQueries({ queryKey: ["release"] });
  return {
    create: useMutation({
      mutationFn: async (b: { agent_id: string; environment: "development" | "staging" | "production"; experiment_id?: string; candidate_version_id?: string; auto_rollback: boolean; min_stage_samples: number; min_stage_seconds: number; reason: string; schedule?: number[] }) =>
        unwrap(await api.POST("/api/v1/releases", { body: { ...b, schedule: b.schedule ?? null, rules: null, seed: null } as never, params: { header: { "Idempotency-Key": idempotencyKey() } } })) as unknown as Plan,
      onSuccess: done,
    }),
    advance: useMutation({ mutationFn: async (id: string) => unwrap(await api.POST("/api/v1/releases/{plan_id}/advance", { params: { path: { plan_id: id } } })), onSuccess: done }),
    rollback: useMutation({ mutationFn: async (v: { id: string; reason: string }) => unwrap(await api.POST("/api/v1/releases/{plan_id}/rollback", { params: { path: { plan_id: v.id } }, body: { reason: v.reason } })), onSuccess: done }),
    patchRules: useMutation({ mutationFn: async (v: { id: string; auto_rollback: boolean }) => unwrap(await api.PATCH("/api/v1/releases/{plan_id}/rules", { params: { path: { plan_id: v.id } }, body: { auto_rollback: v.auto_rollback } as never })), onSuccess: done }),
  };
}
