"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Download, Play } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { DecisionBadge } from "@/components/autopilot/decision";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/inputs";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api, downloadPath, idempotencyKey, unwrap } from "@/lib/api";
import { rate, useDatasets, useEvaluationResults, useEvaluationRuns, useVersions } from "@/lib/autopilot";
import { canWrite, useAuth } from "@/lib/auth";
import { formatCost, formatDuration, relativeTime, shortId } from "@/lib/format";
import { useAgents } from "@/lib/queries";
import type { EvalMetrics } from "@/types/autopilot";

export function Evaluations() {
  const datasets = useDatasets();
  const runs = useEvaluationRuns();
  const agents = useAgents();
  const params = useSearchParams();
  const router = useRouter();
  const selected = params.get("run");
  const { role } = useAuth();
  if (datasets.isLoading || runs.isLoading) return <LoadingBlock rows={6} />;
  if (datasets.error || runs.error) return <ErrorState error={datasets.error ?? runs.error} />;
  const names = new Map((agents.data?.items ?? []).map((a) => [a.id, a.name]));
  const dsById = new Map((datasets.data?.items ?? []).map((d) => [d.id, d]));
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-lg font-semibold">OpenEval</h2>
        <p className="text-sm text-muted-foreground">
          Deterministic evaluators decide pass/fail. Every rate shows its denominator. Each case runs as a pinned workflow run.
        </p>
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader><CardTitle>Datasets</CardTitle><CardDescription>Versioned and immutable; hidden splits are never shown to the optimizer</CardDescription></CardHeader>
          <CardContent>
            <Table data-testid="datasets">
              <THead><tr><TH>Suite</TH><TH>Split</TH><TH>Version</TH><TH>Visibility</TH><TH>Cases</TH><TH>Hash</TH></tr></THead>
              <TBody>{datasets.data?.items.map((d) => (
                <TR key={d.id}><TD>{d.name}</TD><TD><Badge tone={d.split === "development" ? "info" : d.split === "validation" ? "primary" : "warning"}>{d.split}</Badge></TD>
                  <TD>v{d.version}</TD><TD>{d.visibility}</TD><TD>{d.case_count}</TD>
                  <TD className="font-mono text-xs text-muted-foreground">{d.dataset_hash.slice(0, 12)}…</TD></TR>))}</TBody>
            </Table>
          </CardContent>
        </Card>
        {canWrite(role) ? <RunForm /> : <Card><CardContent className="pt-4 text-sm text-muted-foreground">Viewers can inspect results but not start evaluations.</CardContent></Card>}
      </div>
      <Card>
        <CardHeader><CardTitle>Evaluation runs</CardTitle></CardHeader>
        <CardContent>
          {runs.data?.items.length ? (
            <Table data-testid="eval-runs">
              <THead><tr><TH>Run</TH><TH>Agent</TH><TH>Dataset</TH><TH>Status</TH><TH>Task success</TH><TH>Schema</TH><TH>Grounding</TH><TH>Cost / success</TH><TH>p95</TH><TH>Created</TH></tr></THead>
              <TBody>{runs.data.items.map((r) => {
                const m = r.metrics as EvalMetrics | null;
                const ds = dsById.get(r.dataset_id);
                return (
                  <TR key={r.id} className={selected === r.id ? "bg-muted/60" : undefined}>
                    <TD><button className="font-mono text-primary hover:underline" onClick={() => router.replace(`/autopilot/evaluations?run=${r.id}`)}>{shortId(r.id)}</button>
                      {r.arm ? <span className="ml-1 text-xs text-muted-foreground">({r.arm})</span> : null}</TD>
                    <TD>{names.get(r.agent_id)} v{(r.conditions as { agent_version?: number }).agent_version}</TD>
                    <TD className="text-xs">{ds ? `${ds.split} v${ds.version}` : "—"}</TD>
                    <TD><DecisionBadge value={r.status} />{r.status !== "completed" ? <span className="ml-1 text-xs text-muted-foreground">{r.progress.graded ?? 0}/{Object.values(r.progress).reduce((a, b) => a + b, 0)}</span> : null}</TD>
                    <TD data-testid="eval-success">{rate(m?.task_success_rate)}</TD>
                    <TD>{rate(m?.schema_compliance)}</TD><TD>{rate(m?.grounding_pass_rate)}</TD>
                    <TD>{m ? (m.cost_per_successful_task_usd == null ? "undefined" : formatCost(m.cost_per_successful_task_usd)) : "—"}</TD>
                    <TD>{formatDuration(m?.latency_ms.p95)}</TD>
                    <TD className="text-muted-foreground">{relativeTime(r.created_at)}</TD>
                  </TR>);
              })}</TBody>
            </Table>
          ) : <EmptyState title="No evaluation runs yet" />}
        </CardContent>
      </Card>
      {selected ? <RunDetail id={selected} /> : null}
    </div>
  );
}

function RunForm() {
  const agents = useAgents();
  const datasets = useDatasets();
  const qc = useQueryClient();
  const router = useRouter();
  const [agentId, setAgentId] = useState("");
  const effectiveAgent = agentId || agents.data?.items.find((a) => a.name === "Extraction Agent")?.id || "";
  const versions = useVersions(effectiveAgent || null);
  const [versionId, setVersionId] = useState("");
  const [datasetId, setDatasetId] = useState("");
  const [trials, setTrials] = useState(1);
  const start = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/evaluations/run", {
      params: { header: { "Idempotency-Key": idempotencyKey() } },
      body: { agent_id: effectiveAgent, agent_version_id: versionId || null,
              dataset_id: datasetId || datasets.data?.items.find((d) => d.split === "development")?.id || "", trials, confirm_cost: false } })),
    onSuccess: (r) => { void qc.invalidateQueries({ queryKey: ["autopilot"] }); router.replace(`/autopilot/evaluations?run=${r.id}`); },
  });
  return (
    <Card>
      <CardHeader><CardTitle>Run an evaluation</CardTitle></CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Field label="Agent" htmlFor="ev-agent">
          <Select id="ev-agent" value={effectiveAgent} onChange={(e) => { setAgentId(e.target.value); setVersionId(""); }}>
            {agents.data?.items.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
          </Select>
        </Field>
        <Field label="Version" htmlFor="ev-version">
          <Select id="ev-version" value={versionId} onChange={(e) => setVersionId(e.target.value)}>
            <option value="">Active version</option>
            {versions.data?.map((v) => <option key={v.id} value={v.id}>v{v.version} · {v.source}{v.is_active ? " (active)" : ""}</option>)}
          </Select>
        </Field>
        <Field label="Dataset" htmlFor="ev-ds">
          <Select id="ev-ds" value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
            <option value="">Default: development split</option>
            {datasets.data?.items.map((d) => <option key={d.id} value={d.id}>{d.name} · {d.split} v{d.version} ({d.case_count})</option>)}
          </Select>
        </Field>
        <Field label="Trials" htmlFor="ev-trials"><Input id="ev-trials" type="number" min={1} max={5} value={trials} onChange={(e) => setTrials(Number(e.target.value))} /></Field>
        <InlineError error={start.error} />
        <Button onClick={() => start.mutate()} disabled={!effectiveAgent || start.isPending} data-testid="start-evaluation"><Play aria-hidden /> Run evaluation</Button>
      </CardContent>
    </Card>
  );
}

function RunDetail({ id }: { id: string }) {
  const { data, error } = useEvaluationResults(id);
  if (error) return <ErrorState error={error} />;
  if (!data) return <LoadingBlock />;
  const c = data.conditions as Record<string, unknown>;
  return (
    <Card data-testid="eval-detail">
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
        <div>
          <CardTitle>Per-case results · {String(c.agent_name)} v{String(c.agent_version)}</CardTitle>
          <CardDescription>
            {String(c.provider)}/{String(c.model)}{c.simulated ? " (simulated mock provider)" : ""} · config {String(c.config_hash).slice(0, 12)} ·
            dataset {String((data.dataset as { split?: string }).split)} v{String((data.dataset as { version?: number }).version)} · seed: {String(c.seed)}
          </CardDescription>
        </div>
        <div className="flex gap-2">
          {(["json", "csv", "md"] as const).map((f) => (
            <Button key={f} size="sm" variant="outline" onClick={() => void downloadPath(`/api/v1/evaluations/runs/${id}/export?format=${f}`, `openeval-${id}.${f}`)}>
              <Download aria-hidden /> {f.toUpperCase()}
            </Button>
          ))}
        </div>
      </CardHeader>
      <CardContent>
        <Table>
          <THead><tr><TH>Case</TH><TH>Trial</TH><TH>Result</TH><TH>Failing checks</TH><TH>Cost</TH><TH>Latency</TH><TH>Trace</TH></tr></THead>
          <TBody>{data.results.map((r) => {
            const failing = Object.entries(r.scores ?? {}).filter(([, v]) => !v.passed).map(([k]) => k);
            return (
              <TR key={`${r.case_key}-${r.attempt}`} data-testid="case-row" data-passed={String(r.passed)}>
                <TD className="font-mono text-xs">{r.case_key}</TD><TD>{r.attempt}</TD>
                <TD>{r.passed == null ? <Badge>not run</Badge> : r.passed ? <Badge tone="success">pass</Badge> : <Badge tone="danger">fail</Badge>}</TD>
                <TD className="max-w-96 text-xs" title={failing.map((k) => (r.scores?.[k]?.details.failures ?? []).join("; ")).join(" | ")}>
                  {r.error?.type ? <span className="text-danger">{r.error.type}{r.error.category ? ` → ${r.error.category}` : ""} </span> : null}{failing.join(", ") || "—"}
                </TD>
                <TD>{formatCost(r.usage?.cost_usd ?? null)}</TD><TD>{formatDuration(r.usage?.latency_ms ?? null)}</TD>
                <TD>{r.workflow_run_id ? <Link className="font-mono text-xs text-primary hover:underline" href={`/runs/${r.workflow_run_id}`}>{shortId(r.workflow_run_id)}</Link> : "—"}</TD>
              </TR>);
          })}</TBody>
        </Table>
      </CardContent>
    </Card>
  );
}
