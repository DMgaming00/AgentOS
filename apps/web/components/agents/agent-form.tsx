"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { InlineError } from "@/components/ui/feedback";
import { Field, Input, Select, Textarea } from "@/components/ui/inputs";
import { api, unwrap } from "@/lib/api";
import { useSystem, useTools } from "@/lib/queries";
import type { Agent } from "@/types";

type Form = {
  name: string; description: string; system_instructions: string; provider: "mock" | "anthropic" | "openai"; model: string;
  model_configuration: string; allowed_tool_ids: string[]; max_steps: number; max_cost_usd: string; timeout_seconds: number;
};

function toForm(a?: Agent): Form {
  return {
    name: a?.name ?? "", description: a?.description ?? "", system_instructions: a?.system_instructions ?? "",
    provider: (a?.provider as Form["provider"]) ?? "mock", model: a?.model ?? "mock-echo",
    model_configuration: JSON.stringify(a?.model_configuration ?? { scenario: "echo" }, null, 2),
    allowed_tool_ids: a?.allowed_tool_ids ?? [], max_steps: a?.max_steps ?? 8, max_cost_usd: String(a?.max_cost_usd ?? "1"),
    timeout_seconds: a?.timeout_seconds ?? 300,
  };
}

export function AgentForm({ agent, onSaved }: { agent?: Agent; onSaved?: (a: Agent) => void }) {
  const qc = useQueryClient();
  const tools = useTools();
  const system = useSystem();
  const [f, setF] = useState<Form>(toForm(agent));
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((p) => ({ ...p, [k]: v }));

  const save = useMutation({
    mutationFn: async () => {
      let config: Record<string, unknown>;
      try {
        config = JSON.parse(f.model_configuration || "{}");
      } catch {
        throw new Error("Model configuration must be valid JSON");
      }
      const body = {
        name: f.name, description: f.description, system_instructions: f.system_instructions, provider: f.provider,
        model: f.model, model_configuration: config, allowed_tool_ids: f.allowed_tool_ids, max_steps: Number(f.max_steps),
        max_cost_usd: f.max_cost_usd, timeout_seconds: Number(f.timeout_seconds),
      };
      return agent
        ? unwrap(await api.PATCH("/api/v1/agents/{agent_id}", { params: { path: { agent_id: agent.id } }, body: body as never }))
        : unwrap(await api.POST("/api/v1/agents", { body: body as never }));
    },
    onSuccess: (a) => {
      void qc.invalidateQueries({ queryKey: ["agents"] });
      void qc.invalidateQueries({ queryKey: ["agent", a.id] });
      onSaved?.(a);
    },
  });

  const providerDefault = f.provider === "anthropic" ? system.data?.providers.anthropic?.default_model : f.provider === "openai" ? system.data?.providers.openai?.default_model : null;
  const notConfigured = f.provider !== "mock" && system.data && !system.data.providers[f.provider]?.configured;

  return (
    <form className="flex flex-col gap-4" onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
      <div className="grid gap-3 md:grid-cols-2">
        <Field label="Name" htmlFor="a-name"><Input id="a-name" required value={f.name} onChange={(e) => set("name", e.target.value)} /></Field>
        <Field label="Description" htmlFor="a-desc"><Input id="a-desc" value={f.description} onChange={(e) => set("description", e.target.value)} /></Field>
        <Field label="Provider" htmlFor="a-provider" hint={notConfigured ? "No API key configured: runs with this agent will fail (no silent mock fallback)." : undefined}>
          <Select id="a-provider" value={f.provider} onChange={(e) => set("provider", e.target.value as Form["provider"])}>
            <option value="mock">mock (deterministic, simulated)</option>
            <option value="anthropic">anthropic</option>
            <option value="openai">openai</option>
          </Select>
        </Field>
        <Field label="Model" htmlFor="a-model" hint={providerDefault ? `Configured default: ${providerDefault}` : f.provider === "mock" ? "mock-<scenario>, e.g. mock-research" : undefined}>
          <Input id="a-model" required value={f.model} onChange={(e) => set("model", e.target.value)} />
        </Field>
      </div>
      <Field label="System instructions" htmlFor="a-instr" hint="Guidance only. Tool permissions are enforced by the platform, not by this prompt.">
        <Textarea id="a-instr" className="min-h-32" value={f.system_instructions} onChange={(e) => set("system_instructions", e.target.value)} />
      </Field>
      <fieldset className="flex flex-col gap-2">
        <legend className="mb-1 text-xs font-medium text-muted-foreground">Allowed tools</legend>
        <div className="grid gap-2 md:grid-cols-2">
          {tools.data?.map((t) => (
            <label key={t.name} className="flex items-start gap-2 rounded-md border p-2 text-sm">
              <input type="checkbox" className="mt-1" checked={f.allowed_tool_ids.includes(t.name)}
                     onChange={(e) => set("allowed_tool_ids", e.target.checked ? [...f.allowed_tool_ids, t.name] : f.allowed_tool_ids.filter((x) => x !== t.name))} />
              <span>
                <span className="font-mono">{t.name}</span>
                {t.requires_approval ? <span className="ml-2 text-xs text-warning">requires approval</span> : null}
                <span className="block text-xs text-muted-foreground">{t.description}</span>
              </span>
            </label>
          ))}
        </div>
      </fieldset>
      <div className="grid gap-3 md:grid-cols-3">
        <Field label="Max steps (model turns)" htmlFor="a-steps"><Input id="a-steps" type="number" min={1} max={50} value={f.max_steps} onChange={(e) => set("max_steps", Number(e.target.value))} /></Field>
        <Field label="Max cost per step (USD)" htmlFor="a-cost"><Input id="a-cost" type="number" min={0} step="0.01" value={f.max_cost_usd} onChange={(e) => set("max_cost_usd", e.target.value)} /></Field>
        <Field label="Timeout (seconds)" htmlFor="a-timeout"><Input id="a-timeout" type="number" min={1} max={3600} value={f.timeout_seconds} onChange={(e) => set("timeout_seconds", Number(e.target.value))} /></Field>
      </div>
      <Field label="Model configuration (JSON)" htmlFor="a-config" hint='e.g. {"effort": "medium", "max_tokens": 16000} or {"scenario": "research"} for mock'>
        <Textarea id="a-config" className="font-mono text-xs" value={f.model_configuration} onChange={(e) => set("model_configuration", e.target.value)} />
      </Field>
      <InlineError error={save.error} />
      <div className="flex justify-end">
        <Button type="submit" disabled={save.isPending}>{save.isPending ? "Saving…" : agent ? "Save changes" : "Create agent"}</Button>
      </div>
    </form>
  );
}
