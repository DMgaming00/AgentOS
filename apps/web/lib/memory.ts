"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import type { AuditEvent, MemoryExperiment, MemoryHit, MemoryRecord, MemoryVersion } from "@/types/memory";
import { api, idempotencyKey, unwrap } from "./api";

export const useRecords = (namespace: string, includeInactive: boolean) =>
  useQuery({
    queryKey: ["memory", "records", namespace, includeInactive],
    queryFn: async () => (unwrap(await api.GET("/api/v1/memory/records", { params: { query: { namespace: namespace || undefined, include_inactive: includeInactive, limit: 200 } } })) as unknown as { items: MemoryRecord[] }).items,
  });

export const useHistory = (id: string | null) =>
  useQuery({
    queryKey: ["memory", "history", id],
    enabled: !!id,
    queryFn: async () => (unwrap(await api.GET("/api/v1/memory/records/{record_id}/history", { params: { path: { record_id: id! } } })) as unknown as { items: MemoryVersion[] }).items,
  });

export const useAudit = (enabled: boolean) =>
  useQuery({
    queryKey: ["memory", "audit"],
    enabled,
    queryFn: async () => (unwrap(await api.GET("/api/v1/memory/audit", { params: { query: { limit: 100 } } })) as unknown as { items: AuditEvent[] }).items,
    refetchInterval: 10_000,
  });

export const useExperiments = () =>
  useQuery({
    queryKey: ["memory", "experiments"],
    queryFn: async () => unwrap(await api.GET("/api/v1/memory/experiments", { params: { query: {} } })) as unknown as { items: MemoryExperiment[]; definitions: Record<string, string> },
    refetchInterval: 4000,
  });

export const useExperiment = (id: string | null) =>
  useQuery({
    queryKey: ["memory", "experiment", id],
    enabled: !!id,
    queryFn: async () => unwrap(await api.GET("/api/v1/memory/experiments/{experiment_id}", { params: { path: { experiment_id: id! } } })) as unknown as MemoryExperiment,
    refetchInterval: (q) => (q.state.data?.status === "running" ? 2000 : false),
  });

export function useMemoryMutations() {
  const qc = useQueryClient();
  const done = () => void qc.invalidateQueries({ queryKey: ["memory"] });
  return {
    create: useMutation({
      mutationFn: async (b: { kind: string; key?: string; content: string; namespace: string; tags: string[] }) =>
        unwrap(await api.POST("/api/v1/memory/records", { body: b as never, params: { header: { "Idempotency-Key": idempotencyKey() } } })),
      onSuccess: done,
    }),
    update: useMutation({
      mutationFn: async (v: { id: string; expected_version: number; content: string }) =>
        unwrap(await api.PATCH("/api/v1/memory/records/{record_id}", { params: { path: { record_id: v.id } }, body: { expected_version: v.expected_version, content: v.content, clear_expiry: false } })),
      onSuccess: done,
    }),
    remove: useMutation({
      mutationFn: async (id: string) => unwrap(await api.DELETE("/api/v1/memory/records/{record_id}", { params: { path: { record_id: id } } })),
      onSuccess: done,
    }),
    search: useMutation({
      mutationFn: async (v: { query: string; namespace?: string }) =>
        unwrap(await api.POST("/api/v1/memory/search", { body: { query: v.query, namespace: v.namespace || null, limit: 5 } })) as unknown as { items: MemoryHit[]; notice: string; method: string },
    }),
    startExperiment: useMutation({
      mutationFn: async (reader: "mock" | "live" = "mock") =>
        unwrap(await api.POST("/api/v1/memory/experiments", { params: { header: { "Idempotency-Key": idempotencyKey() }, query: { reader } } })) as unknown as MemoryExperiment,
      onSuccess: done,
    }),
  };
}
