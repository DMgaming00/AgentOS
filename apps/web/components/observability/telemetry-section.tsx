"use client";

import { HistogramChart, Heatmap, DependencyGraph } from "@/components/charts/ops-charts";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatCard } from "@/components/ui/stat";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useMetrics } from "@/lib/queries";
import type { Dist, Telemetry } from "@/types/ops";

const ms = (v: number | null | undefined) => (v == null ? "n/a" : v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${v.toFixed(v < 10 ? 1 : 0)} ms`);
const sec = (v: number | null | undefined) => (v == null ? "n/a" : v >= 60 ? `${(v / 60).toFixed(1)} min` : `${v.toFixed(v < 10 ? 2 : 1)} s`);
const pct = (v: number | null | undefined) => (v == null ? "n/a" : `${(v * 100).toFixed(1)}%`);
const q = (d: Dist, f: (v: number | null) => string) => `${f(d.p50)} / ${f(d.p95)}`;

/** Unified telemetry from stored records (Phase 11): distributions, rates, approvals, recovery, heatmap, dependencies. */
export function TelemetrySection({ days }: { days: number }) {
  const t = useMetrics<Telemetry>("telemetry", days);
  if (t.isLoading) return <LoadingBlock rows={4} />;
  if (t.error) return <ErrorState error={t.error} onRetry={() => void t.refetch()} />;
  const d = t.data!;
  return (
    <section className="flex flex-col gap-4" data-testid="telemetry" aria-label="Platform telemetry">
      <div>
        <h2 className="text-lg font-semibold">Platform telemetry</h2>
        <p className="text-sm text-muted-foreground">Distributions and rates over the last {d.window_days} days, computed from stored attempts, model usage, tool executions, approvals and workers (p50 / p95).</p>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
        <StatCard label="Workflow duration" value={q(d.workflow_duration_s, sec)} hint={`${d.workflow_duration_s.n} finished runs`} />
        <StatCard label="Queue latency" value={q(d.queue_latency_ms, ms)} hint={`${d.queue_latency_ms.n} attempts`} />
        <StatCard label="Model-call latency" value={q(d.model_latency_ms.overall, ms)} hint={`${d.model_latency_ms.overall.n} calls`} />
        <StatCard label="Retry rate" value={pct(d.rates.retry_rate)} hint={`${d.rates.attempts} attempts`} />
        <StatCard label="Run failure rate" value={pct(d.rates.run_failure_rate)} hint={`${d.rates.terminal_runs} finished runs`} />
        <StatCard label="Worker utilization" value={pct(d.workers.utilization)} hint={`${d.workers.in_flight}/${d.workers.capacity} slots · ${d.workers.live} live`} />
        <StatCard label="Approval wait" value={q(d.approval_wait_s, sec)} hint={`${d.approval_wait_s.n} decided · ${d.approval_wait_s.pending} pending`} />
        <StatCard label="Recovery duration" value={q(d.recovery_ms, ms)} hint={`${d.recovery_ms.n} lease-expiry recoveries`} />
        <StatCard label="Tokens in / out" value={`${d.tokens.input.toLocaleString()} / ${d.tokens.output.toLocaleString()}`} hint={d.tokens.calls_missing_usage ? `${d.tokens.calls_missing_usage} calls without usage` : `${d.tokens.calls} calls`} />
        <StatCard label="Estimated cost" value={`$${d.estimated_cost.usd.toFixed(4)}`} hint={d.estimated_cost.label} />
        <StatCard label="DB probe (SELECT 1)" value={q({ ...d.queue_latency_ms, p50: d.database_latency.select_1_ms.p50, p95: d.database_latency.select_1_ms.p95 }, ms)} hint="live probe, not workload" />
        <StatCard label="DB probe (indexed read)" value={q({ ...d.queue_latency_ms, p50: d.database_latency.indexed_read_ms.p50, p95: d.database_latency.indexed_read_ms.p95 }, ms)} hint={`${d.database_latency.samples} samples`} />
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <Card><CardHeader><CardTitle className="text-base">Workflow duration</CardTitle></CardHeader><CardContent><HistogramChart buckets={d.workflow_duration_s.histogram} unit="s" label="Runs" /></CardContent></Card>
        <Card><CardHeader><CardTitle className="text-base">Queue latency</CardTitle><CardDescription>Time a step waited for a worker.</CardDescription></CardHeader><CardContent><HistogramChart buckets={d.queue_latency_ms.histogram} unit="ms" label="Attempts" /></CardContent></Card>
        <Card><CardHeader><CardTitle className="text-base">Model-call latency</CardTitle></CardHeader><CardContent><HistogramChart buckets={d.model_latency_ms.overall.histogram} unit="ms" label="Calls" /></CardContent></Card>
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card><CardHeader><CardTitle className="text-base">Failure heatmap</CardTitle><CardDescription>Failed attempts by agent and error type.</CardDescription></CardHeader><CardContent>
          <Heatmap cells={d.failure_heatmap.map((h) => ({ row: h.agent, col: h.error, count: h.count }))} rowKey="agent" colKey="error type" />
        </CardContent></Card>
        <Card><CardHeader><CardTitle className="text-base">Agent → tool dependencies</CardTitle></CardHeader><CardContent><DependencyGraph edges={d.dependency_graph} /></CardContent></Card>
      </div>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card><CardHeader><CardTitle className="text-base">Tool latency</CardTitle></CardHeader><CardContent>
          <Table data-testid="tool-latency"><THead><tr><TH>Tool</TH><TH>Calls</TH><TH>p50</TH><TH>p95</TH><TH>max</TH></tr></THead>
            <TBody>{Object.entries(d.tool_latency_ms).map(([k, v]) => <TR key={k}><TD>{k}</TD><TD>{v.n}</TD><TD>{ms(v.p50)}</TD><TD>{ms(v.p95)}</TD><TD>{ms(v.max)}</TD></TR>)}</TBody></Table>
          {!Object.keys(d.tool_latency_ms).length ? <p className="text-sm text-muted-foreground">No successful tool calls in this window.</p> : null}
        </CardContent></Card>
        <Card><CardHeader><CardTitle className="text-base">Evaluation summaries</CardTitle><CardDescription>Latest completed OpenEval runs.</CardDescription></CardHeader><CardContent>
          <Table data-testid="eval-summaries"><THead><tr><TH>Agent</TH><TH>Dataset</TH><TH>Success</TH><TH>Cases</TH></tr></THead>
            <TBody>{d.evaluations.map((e) => <TR key={e.id}><TD>{e.agent}</TD><TD className="text-xs">{e.dataset}</TD><TD><Badge tone={(e.success ?? 0) >= 0.8 ? "success" : (e.success ?? 0) >= 0.5 ? "warning" : "danger"}>{pct(e.success)}</Badge></TD><TD>{e.cases ?? "—"}</TD></TR>)}</TBody></Table>
          {!d.evaluations.length ? <p className="text-sm text-muted-foreground">No completed evaluations yet.</p> : null}
        </CardContent></Card>
      </div>
      <p className="text-xs text-muted-foreground">{d.database_latency.note}</p>
    </section>
  );
}
