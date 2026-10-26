"use client";

import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, Sparkles, XCircle } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { InlineError } from "@/components/ui/feedback";
import { Field, Input, Select, Textarea } from "@/components/ui/inputs";
import { api, unwrap } from "@/lib/api";
import { useSystem } from "@/lib/queries";
import type { PlanResponse, ValidateResponse } from "@/types";
import { DagGraph, type DagNodeSpec } from "./dag-graph";

const EXAMPLE = {
  nodes: [
    { key: "research", type: "agent_task", agent: "Research Agent", tools: ["document_search"], input: { platform: "Nimbus Inference" } },
    { key: "summary", type: "aggregation", depends_on: ["research"] },
  ],
  budget: { max_model_calls: 10, max_cost_usd: 1 },
};

export function WorkflowEditor({ initial }: { initial?: { name: string; description: string; definition: object } }) {
  const router = useRouter();
  const system = useSystem();
  const [task, setTask] = useState(
    "Research and compare three AI inference platforms using the workspace documents, compare their numbers, and write a Markdown report.",
  );
  const [provider, setProvider] = useState<string>("");
  const [name, setName] = useState(initial?.name ?? "my-workflow");
  const [description, setDescription] = useState(initial?.description ?? "");
  const [text, setText] = useState(JSON.stringify(initial?.definition ?? EXAMPLE, null, 2));
  const [validation, setValidation] = useState<ValidateResponse | null>(null);
  const [planInfo, setPlanInfo] = useState<PlanResponse | null>(null);

  const parsed = useMemo(() => {
    try {
      return { value: JSON.parse(text) as { nodes?: DagNodeSpec[] }, error: null };
    } catch (e) {
      return { value: null, error: (e as Error).message };
    }
  }, [text]);

  const validate = useMutation({
    mutationFn: async (definition: object) => unwrap(await api.POST("/api/v1/workflows/validate", { body: { definition: definition as Record<string, never> } })),
    onSuccess: setValidation,
  });
  useEffect(() => {
    if (!parsed.value) return;
    const t = setTimeout(() => validate.mutate(parsed.value as object), 400);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text]);

  const plan = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/workflows/plan", {
      body: { task, provider: (provider || null) as "mock" | "anthropic" | "openai" | null },
    })),
    onSuccess: (res) => {
      setPlanInfo(res);
      if (res.definition) setText(JSON.stringify(res.definition, null, 2));
    },
  });

  const save = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/workflows", {
      body: { name, description, definition: parsed.value as Record<string, never> },
    })),
    onSuccess: (wf) => router.push(`/workflows/${wf.id}`),
  });

  const nodes = Array.isArray(parsed.value?.nodes) ? (parsed.value!.nodes as DagNodeSpec[]).filter((n) => n && typeof n.key === "string") : [];

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2"><Sparkles className="size-4" aria-hidden /> Plan from a natural-language task</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <Field label="Task" htmlFor="task">
            <Textarea id="task" value={task} onChange={(e) => setTask(e.target.value)} />
          </Field>
          <div className="flex flex-wrap items-end gap-3">
            <div className="w-56">
              <Field label="Planner provider" htmlFor="provider">
                <Select id="provider" value={provider} onChange={(e) => setProvider(e.target.value)}>
                  <option value="">Default ({system.data?.planner_provider ?? "…"})</option>
                  <option value="mock">mock (deterministic, simulated)</option>
                  <option value="anthropic" disabled={!system.data?.providers.anthropic?.configured}>anthropic{system.data?.providers.anthropic?.configured ? "" : " (no API key)"}</option>
                  <option value="openai" disabled={!system.data?.providers.openai?.configured}>openai{system.data?.providers.openai?.configured ? "" : " (no API key)"}</option>
                </Select>
              </Field>
            </div>
            <Button onClick={() => plan.mutate()} disabled={plan.isPending || task.length < 3}>
              {plan.isPending ? "Planning…" : "Generate plan"}
            </Button>
            {planInfo ? (
              <span className="text-xs text-muted-foreground">
                {planInfo.provider}/{planInfo.model}{planInfo.simulated ? " (simulated)" : ""} · {planInfo.attempts.length} attempt(s) ·{" "}
                {planInfo.valid ? "valid" : "invalid after bounded replanning"}
              </span>
            ) : null}
          </div>
          <InlineError error={plan.error} />
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Definition (JSON)</CardTitle></CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Name" htmlFor="wf-name"><Input id="wf-name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
              <Field label="Description" htmlFor="wf-desc"><Input id="wf-desc" value={description} onChange={(e) => setDescription(e.target.value)} /></Field>
            </div>
            <label htmlFor="wf-json" className="sr-only">Workflow JSON</label>
            <Textarea id="wf-json" value={text} onChange={(e) => setText(e.target.value)} spellCheck={false}
                      className="min-h-[420px] font-mono text-xs" data-testid="workflow-json" />
            {parsed.error ? <p className="text-xs text-danger" role="alert">JSON error: {parsed.error}</p> : null}
          </CardContent>
        </Card>
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Proposed DAG</CardTitle>
              {validation ? (validation.valid ? <Badge tone="success"><CheckCircle2 className="size-3" /> valid</Badge> :
                <Badge tone="danger"><XCircle className="size-3" /> {validation.issues.length} issue(s)</Badge>) : null}
            </CardHeader>
            <CardContent>
              {nodes.length ? <DagGraph nodes={nodes} height={340} /> : <p className="text-sm text-muted-foreground">Add nodes to see the graph.</p>}
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Validation</CardTitle></CardHeader>
            <CardContent className="flex flex-col gap-3">
              {validation?.issues.length ? (
                <ul className="flex flex-col gap-1 text-sm" data-testid="validation-issues">
                  {validation.issues.map((i, n) => (
                    <li key={n} className="flex gap-2"><Badge tone="danger">{i.code}</Badge><span>{i.node ? <span className="font-mono">{i.node}: </span> : null}{i.message}</span></li>
                  ))}
                </ul>
              ) : validation ? <p className="text-sm text-success">No issues. Execution order: {validation.topological_order.join(" → ")}</p> :
                <p className="text-sm text-muted-foreground">Validating…</p>}
              <Button onClick={() => save.mutate()} disabled={!validation?.valid || save.isPending || !name}>
                {save.isPending ? "Saving…" : "Save workflow"}
              </Button>
              <InlineError error={save.error} />
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
