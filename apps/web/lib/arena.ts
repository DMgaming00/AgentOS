"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { ArenaDetail, ArenaSummary, CaseResult, Contender, Corpus, Split } from "@/types/arena";
import { api, idempotencyKey, unwrap } from "./api";

export const useCorpus = () =>
  useQuery({ queryKey: ["arena", "corpus"], queryFn: async () => unwrap(await api.GET("/api/v1/arena/corpus")) as unknown as Corpus });

export const useContenders = () =>
  useQuery({ queryKey: ["arena", "contenders"], queryFn: async () => (unwrap(await api.GET("/api/v1/arena/contenders")) as unknown as { items: Contender[] }).items });

export const useArenaRuns = () =>
  useQuery({
    queryKey: ["arena", "runs"],
    queryFn: async () => (unwrap(await api.GET("/api/v1/arena/runs", { params: { query: {} } })) as unknown as { items: ArenaSummary[] }).items,
    refetchInterval: 5000,
  });

export const useArena = (id: string | null) =>
  useQuery({
    queryKey: ["arena", "run", id],
    enabled: !!id,
    queryFn: async () => unwrap(await api.GET("/api/v1/arena/runs/{arena_id}", { params: { path: { arena_id: id! } } })) as unknown as ArenaDetail,
    refetchInterval: (q) => (q.state.data && ["selection_running", "final_test_running"].includes(q.state.data.stage) ? 2000 : false),
  });

export const useArenaCases = (id: string | null, split: Split, versionId: string | null, enabled: boolean) =>
  useQuery({
    queryKey: ["arena", "cases", id, split, versionId],
    enabled: !!id && enabled,
    queryFn: async () =>
      (unwrap(await api.GET("/api/v1/arena/runs/{arena_id}/cases", { params: { path: { arena_id: id! }, query: { split, agent_version_id: versionId || undefined, failed_only: true } } })) as unknown as { items: CaseResult[]; expected_hidden: boolean }),
  });

export function useCreateArena() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (v: { name: string; agent_version_ids: string[] }) =>
      unwrap(await api.POST("/api/v1/arena/runs", { body: { ...v, allow_live: false }, params: { header: { "Idempotency-Key": idempotencyKey() } } })) as unknown as ArenaSummary,
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["arena"] }),
  });
}

export function useFinalTest(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/arena/runs/{arena_id}/final-test", { params: { path: { arena_id: id }, header: { "Idempotency-Key": idempotencyKey() } } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["arena"] }),
  });
}
