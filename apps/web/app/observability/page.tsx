"use client";

import Link from "next/link";
import { useState } from "react";

import { HBarChart, SeriesLineChart, StackedBarChart } from "@/components/charts/charts";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { Select } from "@/components/ui/inputs";
import { StatCard } from "@/components/ui/stat";
import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { formatCost, formatDuration, relativeTime, shortId } from "@/lib/format";
import { useMetrics } from "@/lib/queries";

type Obs = {
  failure_breakdown: { error_type: string; count: number }[];
  attempts_by_status: Record<string, number>;
  retries: number;
  queue_wait_ms: { p50: number | null; p95: number | null };
  step_duration_by_type: { node_type: string; count: number; p50_ms: number | null; p95_ms: number | null }[];
  tool_activity: Record<string, Record<string, number>>;
  model_latency_hourly: { hour: string; provider: string; p50_ms: number | null; p95_ms: number | null; calls: number }[];
  recent_errors: { run_id: string; workflow: string; event_type: string; timestamp: string; node_key?: string; error?: { type?: string; message?: string }; trace_id?: string }[];
  timeline: { run_id: string; workflow: string; status: string; started_at: string | null; completed_at: string | null; cost_usd: number }[];
  workers: { id: string; hostname: string; status: string; concurrency: number; in_flight: number; steps_completed: number; last_heartbeat_at: string; healthy: boolean }[];
};
type Cost = { total_estimated_cost_usd: number; note: string; by_agent: { agent: string; calls: number; estimated_cost_usd: number | null }[];
  cost_basis_counts: Record<string, number>; most_expensive_runs: { run_id: string; workflow: string; status: string; estimated_cost_usd: number; model_calls: number }[] };
type Usage = { totals: { calls: number; input_tokens: number; output_tokens: number; calls_missing_usage: number };
  by_model: { provider: string; model: string; calls: number; latency_p50_ms: number | null; latency_p95_ms: number | null; failed_calls: number }[];
  provider_distribution: Record<string, number> };

export default function ObservabilityPage() {
  const [days, setDays] = useState(7);
  const obs = useMetrics<Obs>("observability", days);
  const cost = useMetrics<Cost>("cost", days);
  const usage = useMetrics<Usage>("usage", days);
  if (obs.isLoading || cost.isLoading || usage.isLoading) return <LoadingBlock rows={8} />;
  const err = obs.error ?? cost.error ?? usage.error;
  if (err) return <ErrorState error={err} onRetry={() => { void obs.refetch(); void cost.refetch(); void usage.refetch(); }} />;
  const o = obs.data!, c = cost.data!, u = usage.data!;
  const latency = o.model_latency_hourly.map((r) => ({
    hour: new Date(r.hour).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit" }) + (r.provider !== "mock" ? ` (${r.provider})` : ""),
    p50: r.p50_ms, p95: r.p95_ms,
  }));
  const tools = Object.entries(o.tool_activity).map(([tool, s]) => ({
    tool, succeeded: s.succeeded ?? 0, failed: (s.failed ?? 0) + (s.denied ?? 0) + (s.rejected ?? 0),
    pending: (s.pending_approval ?? 0) + (s.approved ?? 0) + (s.running ?? 0),
  }));
  const simulated = (c.cost_basis_counts.simulated ?? 0) > 0;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Observability</h1>
          <p className="text-sm text-muted-foreground">All figures computed from persisted attempts, model usage, tool executions and events.</p>
        </div>
        <div className="w-40">
          <label htmlFor="window" className="sr-only">Time window</label>
          <Select id="window" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            <option value={1}>Last 24 hours</option><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option>
          </Select>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        <StatCard label="Model calls" value={u.totals.calls} hint={u.totals.calls_missing_usage ? `${u.totals.calls_missing_usage} without usage data` : "all reported usage"} />
        <StatCard label="Tokens in / out" value={`${u.totals.input_tokens.toLocaleString()} / ${u.totals.output_tokens.toLocaleString()}`} />
        <StatCard label="Estimated cost" value={formatCost(c.total_estimated_cost_usd)} hint={simulated ? "includes simulated (mock) pricing" : "estimate, not invoice"} />
        <StatCard label="Retried attempts" value={o.retries} tone={o.retries ? "warning" : undefined} hint="failed + lease expired" />
        <StatCard label="Queue wait p50 / p95" value={`${formatDuration(o.queue_wait_ms.p50)} / ${formatDuration(o.queue_wait_ms.p95)}`} />
        <StatCard label="Healthy workers" value={o.workers.filter((w) => w.healthy).length} hint={`${o.workers.length} seen`} />
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Model call latency</CardTitle><CardDescription>p50 and p95 per hour, successful calls</CardDescription></CardHeader>
          <CardContent>
            {latency.length ? <SeriesLineChart data={latency} x="hour" format={(v) => formatDuration(v)}
              series={[{ key: "p50", label: "p50", color: "var(--series-1)" }, { key: "p95", label: "p95", color: "var(--series-2)" }]} />
              : <EmptyState title="No model calls in this window" />}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Estimated cost by agent</CardTitle><CardDescription>{c.note}</CardDescription></CardHeader>
          <CardContent>
            {c.by_agent.length ? <HBarChart data={c.by_agent.map((a) => ({ agent: a.agent, cost: a.estimated_cost_usd ?? 0 }))} category="agent" value="cost" valueLabel="Estimated cost" format={(v) => formatCost(v)} />
              : <EmptyState title="No cost recorded" />}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Failed attempts by error type</CardTitle><CardDescription>Including retried (transient) failures</CardDescription></CardHeader>
          <CardContent>
            {o.failure_breakdown.length ? <HBarChart data={o.failure_breakdown.map((f) => ({ error: f.error_type, count: f.count }))} category="error" value="count" valueLabel="Attempts" color="var(--series-2)" />
              : <EmptyState title="No failures in this window" />}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Tool activity</CardTitle><CardDescription>Failed includes denied and rejected calls</CardDescription></CardHeader>
          <CardContent>
            {tools.length ? <StackedBarChart data={tools} category="tool" series={[
              { key: "succeeded", label: "Succeeded", color: "var(--series-1)" },
              { key: "failed", label: "Failed / denied / rejected", color: "var(--series-2)" },
              { key: "pending", label: "Pending / in progress", color: "var(--series-3)" }]} />
              : <EmptyState title="No tool calls" />}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Step duration by node type</CardTitle></CardHeader>
          <CardContent>
            <Table><THead><tr><TH>Type</TH><TH>Attempts</TH><TH>p50</TH><TH>p95</TH></tr></THead>
              <TBody>{o.step_duration_by_type.map((r) => <TR key={r.node_type}><TD>{r.node_type}</TD><TD>{r.count}</TD><TD>{formatDuration(r.p50_ms)}</TD><TD>{formatDuration(r.p95_ms)}</TD></TR>)}</TBody></Table>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Models</CardTitle></CardHeader>
          <CardContent>
            <Table><THead><tr><TH>Provider / model</TH><TH>Calls</TH><TH>Failed</TH><TH>p50</TH><TH>p95</TH></tr></THead>
              <TBody>{u.by_model.map((m) => <TR key={m.provider + m.model}><TD className="text-xs">{m.provider}/{m.model}</TD><TD>{m.calls}</TD><TD>{m.failed_calls}</TD><TD>{formatDuration(m.latency_p50_ms)}</TD><TD>{formatDuration(m.latency_p95_ms)}</TD></TR>)}</TBody></Table>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle>Recent errors</CardTitle><CardDescription>Trace ids correlate with OpenTelemetry spans when export is enabled</CardDescription></CardHeader>
        <CardContent>
          {o.recent_errors.length ? (
            <Table><THead><tr><TH>When</TH><TH>Event</TH><TH>Run / step</TH><TH>Error</TH><TH>Trace</TH></tr></THead>
              <TBody>{o.recent_errors.map((e, i) => (
                <TR key={i}>
                  <TD className="text-muted-foreground">{relativeTime(e.timestamp)}</TD>
                  <TD className="font-mono text-xs">{e.event_type}</TD>
                  <TD><Link className="text-primary hover:underline" href={`/runs/${e.run_id}${e.node_key ? `?step=${e.node_key}` : ""}`}>{e.workflow} {shortId(e.run_id)}</Link>
                    {e.node_key ? <span className="block font-mono text-xs text-muted-foreground">{e.node_key}</span> : null}</TD>
                  <TD className="max-w-96 truncate text-xs" title={e.error?.message}>{e.error?.type ?? "—"} {e.error?.message ? `— ${e.error.message}` : ""}</TD>
                  <TD className="font-mono text-[11px] text-muted-foreground">{e.trace_id ? e.trace_id.slice(0, 16) : "—"}</TD>
                </TR>))}</TBody></Table>
          ) : <p className="text-sm text-muted-foreground">No errors in this window.</p>}
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Workers</CardTitle></CardHeader>
          <CardContent>
            <Table><THead><tr><TH>Worker</TH><TH>Status</TH><TH>In flight</TH><TH>Completed</TH><TH>Heartbeat</TH></tr></THead>
              <TBody>{o.workers.map((w) => (
                <TR key={w.id}><TD className="font-mono text-xs">{w.id}</TD>
                  <TD>{w.healthy ? <Badge tone="success">healthy</Badge> : <Badge tone={w.status === "stopped" ? "neutral" : "danger"}>{w.status === "active" ? "missed heartbeats" : w.status}</Badge>}</TD>
                  <TD>{w.in_flight}/{w.concurrency}</TD><TD>{w.steps_completed}</TD><TD className="text-muted-foreground">{relativeTime(w.last_heartbeat_at)}</TD></TR>))}</TBody></Table>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Most expensive runs</CardTitle></CardHeader>
          <CardContent>
            <Table><THead><tr><TH>Run</TH><TH>Status</TH><TH>Model calls</TH><TH>Cost</TH></tr></THead>
              <TBody>{c.most_expensive_runs.map((r) => (
                <TR key={r.run_id}><TD><Link className="text-primary hover:underline" href={`/runs/${r.run_id}`}>{r.workflow} {shortId(r.run_id)}</Link></TD>
                  <TD><StatusBadge status={r.status} /></TD><TD>{r.model_calls}</TD><TD>{formatCost(r.estimated_cost_usd)}</TD></TR>))}</TBody></Table>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
