"use client";

import Link from "next/link";
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";

import { FlightPlayer } from "@/components/flight/player";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api, unwrap } from "@/lib/api";
import { useFlight } from "@/lib/flight";
import { shortId } from "@/lib/format";
import type { Inspector } from "@/types/ops";

export const useInspector = (id: string) =>
  useQuery({ queryKey: ["run-inspector", id], queryFn: async () => unwrap(await api.GET("/api/v1/runs/{run_id}/inspector", { params: { path: { run_id: id } } })) as unknown as Inspector, refetchInterval: 4000 });

const KIND_COLOR: Record<string, string> = { run: "var(--foreground)", step: "var(--series-1)", attempt: "var(--muted-foreground)", model_call: "var(--series-3)", tool_call: "var(--series-2)", approval: "var(--warning)" };
const usd = (v: number | null | undefined, d = 6) => (v == null ? "n/a" : `$${v.toFixed(d)}`);
const ms = (v: number | null | undefined) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`);

function Gate({ q, children }: { q: ReturnType<typeof useInspector>; children: (d: Inspector) => React.ReactNode }) {
  if (q.isLoading) return <LoadingBlock rows={4} />;
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  return <>{q.data ? children(q.data) : null}</>;
}

export function FlightTab({ id }: { id: string }) {
  const f = useFlight(id);
  if (f.isLoading) return <LoadingBlock rows={6} label="Loading recording" />;
  if (f.error) return <ErrorState error={f.error} onRetry={() => void f.refetch()} />;
  return f.data ? <FlightPlayer rec={f.data} key={f.data.run.id} /> : null;
}

export function TraceTab({ id }: { id: string }) {
  const q = useInspector(id);
  return (
    <Gate q={q}>
      {(d) => <Waterfall d={d} />}
    </Gate>
  );
}

function Waterfall({ d }: { d: Inspector }) {
  const rows = useMemo(() => {
    const byId = new Map(d.spans.map((s) => [s.id, s]));
    const depth = (s: Inspector["spans"][number]) => { let n = 0, p = s.parent; while (p && byId.has(p) && n < 8) { n += 1; p = byId.get(p)!.parent; } return n; };
    const t0 = Math.min(...d.spans.map((s) => (s.start ? Date.parse(s.start) : Infinity)));
    const t1 = Math.max(...d.spans.map((s) => (s.end ? Date.parse(s.end) : s.start ? Date.parse(s.start) : 0)), t0 + 1);
    // parents before children, in time order
    const order = [...d.spans].sort((a, b) => (a.start ?? "").localeCompare(b.start ?? "") || depth(a) - depth(b));
    return { t0, span: Math.max(1, t1 - t0), items: order.map((s) => ({ s, depth: depth(s) })) };
  }, [d]);
  return (
    <div className="flex flex-col gap-3" data-testid="trace-tab">
      <Card>
        <CardHeader><CardTitle className="text-base">Correlation</CardTitle><CardDescription>{d.correlation.note}</CardDescription></CardHeader>
        <CardContent className="flex flex-wrap gap-3 text-xs">
          <span>Run <span className="font-mono">{d.run.id}</span></span>
          <span>Trace <span className="font-mono" data-testid="trace-id">{d.correlation.trace_id ?? "none recorded"}</span></span>
          <span>{d.correlation.span_ids_in_events} span ids in events</span>
          <Badge tone={d.integrity.gap_free && d.integrity.monotonic ? "success" : "danger"} data-testid="integrity">events {d.integrity.first_sequence}–{d.integrity.last_sequence}: {d.integrity.gap_free && d.integrity.monotonic ? "gap-free, monotonic" : "GAP or reordering"}</Badge>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle className="text-base">Span waterfall</CardTitle>
          <div className="flex flex-wrap gap-3 text-[11px]">{Object.entries(KIND_COLOR).map(([k, c]) => <span key={k} className="flex items-center gap-1"><span className="inline-block size-2 rounded-sm" style={{ background: c }} />{k.replace("_", " ")}</span>)}</div>
        </CardHeader>
        <CardContent>
          <div role="table" aria-label="Spans" data-testid="waterfall" className="flex flex-col gap-0.5 text-xs">
            {rows.items.map(({ s, depth }) => {
              const left = s.start ? ((Date.parse(s.start) - rows.t0) / rows.span) * 100 : 0;
              const width = Math.max(0.6, ((s.duration_ms ?? 0) / rows.span) * 100);
              return (
                <div key={s.id} role="row" data-kind={s.kind} data-status={s.status ?? ""} className="grid grid-cols-[minmax(160px,260px)_1fr_70px] items-center gap-2">
                  <span role="cell" className="truncate" style={{ paddingLeft: depth * 12 }} title={`${s.kind}: ${s.label}`}><span className="text-muted-foreground">{s.kind.replace("_", " ")}</span> {s.label}</span>
                  <div className="relative h-3 rounded-sm bg-muted/40" role="cell" aria-hidden><div className="absolute top-0 h-3 rounded-sm" style={{ left: `${left}%`, width: `${Math.min(100 - left, width)}%`, background: KIND_COLOR[s.kind] }} /></div>
                  <span role="cell" className="text-right text-muted-foreground">{ms(s.duration_ms)}</span>
                </div>);
            })}
          </div>
          <details className="mt-3 text-xs"><summary className="cursor-pointer text-muted-foreground">View as table</summary>
            <Table><THead><tr><TH>Kind</TH><TH>Span</TH><TH>Status</TH><TH>Duration</TH><TH>Parent</TH></tr></THead>
              <TBody>{d.spans.map((s) => <TR key={s.id}><TD>{s.kind}</TD><TD>{s.label}</TD><TD>{s.status}</TD><TD>{ms(s.duration_ms)}</TD><TD className="font-mono text-[11px]">{s.parent ? shortId(s.parent) : "—"}</TD></TR>)}</TBody></Table></details>
        </CardContent>
      </Card>
    </div>
  );
}

export function ToolsTab({ id }: { id: string }) {
  const q = useInspector(id);
  return (
    <Gate q={q}>
      {(d) => d.tools.length === 0 ? <EmptyState title="No tool calls" description="This run made no tool calls." /> : (
        <Table data-testid="tools-tab">
          <THead><tr><TH>Tool</TH><TH>Status</TH><TH>Duration</TH><TH>Approval</TH><TH>Args hash</TH><TH>Error</TH></tr></THead>
          <TBody>{d.tools.map((t) => <TR key={t.id}><TD>{t.tool}</TD><TD><Badge tone={t.status === "succeeded" ? "success" : t.status === "denied" || t.status === "failed" ? "danger" : "neutral"}>{t.status}</Badge></TD><TD>{ms(t.duration_ms)}</TD><TD>{t.requires_approval ? "required" : "—"}</TD><TD className="font-mono text-[11px]">{t.arguments_hash}</TD><TD className="text-xs">{t.error ?? "—"}</TD></TR>)}</TBody>
        </Table>)}
    </Gate>
  );
}

export function EvaluationTab({ id }: { id: string }) {
  const q = useInspector(id);
  return (
    <Gate q={q}>
      {(d) => d.evaluation.length === 0 ? <EmptyState title="Not an evaluation run" description="Evaluation results appear here when this run executed an OpenEval case." /> : (
        <div className="flex flex-col gap-2" data-testid="evaluation-tab">
          {d.evaluation.map((e) => (
            <Card key={e.evaluation_run_id + e.case_key}><CardHeader><CardTitle className="text-base">{e.dataset} · case {e.case_key} <Badge tone={e.passed ? "success" : "danger"}>{e.passed ? "passed" : "failed"}</Badge></CardTitle>
              <CardDescription><Link className="text-primary hover:underline" href="/autopilot/evaluations">Evaluation run {shortId(e.evaluation_run_id)}</Link></CardDescription></CardHeader>
              <CardContent className="flex flex-wrap gap-2 text-xs">{Object.entries(e.scores).map(([m, v]) => <Badge key={m} tone={v.passed ? "success" : "danger"}>{m.replace(/_/g, " ")}</Badge>)}{e.error ? <span className="text-muted-foreground">error: {String((e.error as { category?: string }).category ?? (e.error as { type?: string }).type)}</span> : null}</CardContent></Card>))}
        </div>)}
    </Gate>
  );
}

export function CostTab({ id }: { id: string }) {
  const q = useInspector(id);
  return (
    <Gate q={q}>
      {(d) => (
        <div className="flex flex-col gap-3" data-testid="cost-tab">
          <p className="text-sm"><Badge tone="warning">{d.cost.label}</Badge> <span className="text-muted-foreground">Estimated from reported token usage and the versioned price table; the run&apos;s budget counter shows {usd(d.cost.run_reported_usd)}.</span></p>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3"><Card className="p-4"><p className="text-xs text-muted-foreground">Total estimated</p><p className="text-2xl font-semibold" data-testid="cost-total">{usd(d.cost.total_usd)}</p></Card>
            <Card className="p-4"><p className="text-xs text-muted-foreground">Spent on failed attempts</p><p className="text-2xl font-semibold">{usd(d.cost.retry_cost_usd)}</p></Card></div>
          <div className="grid gap-3 lg:grid-cols-2">
            <Card><CardHeader><CardTitle className="text-base">By step</CardTitle></CardHeader><CardContent><Table><THead><tr><TH>Step</TH><TH>Cost</TH></tr></THead><TBody>{d.cost.by_step.map((s) => <TR key={s.step_id ?? "x"}><TD>{s.node_key ?? "(none)"}</TD><TD>{usd(s.usd)}</TD></TR>)}</TBody></Table></CardContent></Card>
            <Card><CardHeader><CardTitle className="text-base">By model</CardTitle></CardHeader><CardContent><Table><THead><tr><TH>Model</TH><TH>Calls</TH><TH>Tokens in/out</TH><TH>Cost</TH></tr></THead><TBody>{d.cost.by_model.map((m) => <TR key={m.model}><TD>{m.model}</TD><TD>{m.calls}</TD><TD className="text-xs">{m.in} / {m.out}</TD><TD>{usd(m.cost)}</TD></TR>)}</TBody></Table></CardContent></Card>
          </div>
        </div>)}
    </Gate>
  );
}

export function DiagnosisTab({ id }: { id: string }) {
  const q = useInspector(id);
  return (
    <Gate q={q}>
      {(d) => d.failure.signals.length === 0 ? <EmptyState title="No failure signals for this run" description="Failure Intelligence has not flagged anything for this run (analysis runs on demand and from the coordinator)." /> : (
        <div className="flex flex-col gap-3" data-testid="diagnosis-tab">
          <Table><THead><tr><TH>Category</TH><TH>Detector</TH><TH>Evidence</TH></tr></THead>
            <TBody>{d.failure.signals.map((s) => <TR key={s.id}><TD><Badge tone={s.infrastructure ? "neutral" : "danger"}>{s.category}</Badge></TD><TD className="text-xs">{s.detector}</TD><TD className="text-xs">{s.summary}</TD></TR>)}</TBody></Table>
          {d.failure.diagnoses.map((x) => <Card key={x.id}><CardHeader><CardTitle className="text-base">Diagnosis: {x.category} <Badge>{x.confidence} confidence</Badge></CardTitle><CardDescription>A hypothesis from trace evidence, not a verified root cause.</CardDescription></CardHeader><CardContent className="text-sm">{x.hypothesis}</CardContent></Card>)}
          <Link href="/autopilot/failures" className="text-sm text-primary hover:underline">Open Failure Intelligence →</Link>
        </div>)}
    </Gate>
  );
}

export function ImprovementsTab({ id }: { id: string }) {
  const q = useInspector(id);
  return (
    <Gate q={q}>
      {(d) => d.candidates.length === 0 ? <EmptyState title="No candidates for the agents in this run" description="Generate candidates in the Optimization Lab once failures are diagnosed." /> : (
        <div className="flex flex-col gap-2" data-testid="improvements-tab">
          {d.candidates.map((c) => <Card key={c.id} className="p-3 text-sm"><div className="flex items-center gap-2"><span className="font-medium">{c.title}</span><Badge>{c.status.replace("_", " ")}</Badge><Badge tone="neutral">{c.rule_id ? `rule ${c.rule_id}` : c.generator}</Badge></div></Card>)}
          <Link href={`/autopilot/improvements?agent=${d.candidates[0].agent_id}`} className="text-sm text-primary hover:underline">Open the Optimization Lab →</Link>
        </div>)}
    </Gate>
  );
}

export function DeploymentTab({ id }: { id: string }) {
  const q = useInspector(id);
  return (
    <Gate q={q}>
      {(d) => d.versions.length === 0 ? <EmptyState title="No agent versions" description="This run had no agent steps, so no agent version was pinned." /> : (
        <div className="flex flex-col gap-3" data-testid="deployment-tab">
          {d.versions.map((v) => (
            <Card key={v.version_id}><CardHeader><CardTitle className="text-base">{v.agent} v{v.version} <Badge tone={v.role_now === "active" ? "success" : v.role_now === "canary" ? "info" : "neutral"}>{v.role_now} now</Badge></CardTitle>
              <CardDescription>Pinned when this run was created ({v.pinned_environment}); hash <span className="font-mono">{v.config_hash}</span>, source {v.source}.</CardDescription></CardHeader>
              <CardContent className="flex flex-col gap-2 text-xs">
                <div>{v.deployments.length ? v.deployments.map((x) => <div key={x.id}>{x.type} · {x.status}{x.type === "canary" ? ` · ${x.canary_percent}%` : ""} · {new Date(x.deployed_at).toLocaleString()}</div>) : "No deployment record."}</div>
                {v.releases.map((r) => <Link key={r.id} href="/releases" className="text-primary hover:underline">Release {shortId(r.id)} ({r.environment}, {r.status}, as {r.role})</Link>)}
              </CardContent></Card>))}
        </div>)}
    </Gate>
  );
}
