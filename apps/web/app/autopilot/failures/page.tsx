"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ScanSearch, Stethoscope } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { StackedBarChart } from "@/components/charts/charts";
import { DecisionBadge } from "@/components/autopilot/decision";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Sheet } from "@/components/ui/dialog";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Select } from "@/components/ui/inputs";
import { KV } from "@/components/ui/json";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { useDiagnosis, useFailures } from "@/lib/autopilot";
import { canWrite, useAuth } from "@/lib/auth";
import { formatCost, relativeTime, shortId } from "@/lib/format";

const SERIES = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];

export default function FailuresPage() {
  const [hours, setHours] = useState(168);
  const { data, error, isLoading, refetch } = useFailures(hours);
  const { role } = useAuth();
  const qc = useQueryClient();
  const [open, setOpen] = useState<string | null>(null);
  const analyze = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/autopilot/failures/analyze", { body: { window_hours: hours } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["autopilot"] }),
  });
  const diagnose = useMutation({
    mutationFn: async (v: { agent_id: string; category: string }) =>
      unwrap(await api.POST("/api/v1/autopilot/diagnoses", {
        params: { header: { "Idempotency-Key": idempotencyKey() } }, body: v })),
    onSuccess: (d) => {
      void qc.invalidateQueries({ queryKey: ["autopilot"] });
      setOpen(d.id);
    },
  });

  const timeline = useMemo(() => {
    if (!data) return { rows: [], series: [] as { key: string; label: string; color: string }[] };
    const totals: Record<string, number> = {};
    for (const t of data.timeline) totals[t.category as string] = (totals[t.category as string] ?? 0) + (t.count as number);
    // Three validated series slots; everything else folds into "Other" (never a generated hue).
    const top = Object.entries(totals).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k]) => k);
    const buckets: Record<string, Record<string, number | string>> = {};
    for (const t of data.timeline) {
      const h = new Date(t.hour as string).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit" });
      const key = top.includes(t.category as string) ? (t.category as string) : "OTHER";
      buckets[h] = buckets[h] ?? { hour: h };
      buckets[h][key] = ((buckets[h][key] as number) ?? 0) + (t.count as number);
    }
    const series = top.map((k, i) => ({ key: k, label: k, color: SERIES[i] }));
    if (Object.keys(totals).length > 3) series.push({ key: "OTHER", label: "Other", color: "var(--muted-foreground)" });
    return { rows: Object.values(buckets), series };
  }, [data]);

  if (isLoading) return <LoadingBlock rows={6} />;
  if (error || !data) return <ErrorState error={error} onRetry={() => void refetch()} />;
  const writer = canWrite(role);

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold">Failure Intelligence</h2>
          <p className="text-sm text-muted-foreground">Deterministic detectors over persisted traces; diagnoses are hypotheses.</p>
        </div>
        <div className="flex items-end gap-2">
          <div className="w-40">
            <label htmlFor="fw" className="sr-only">Window</label>
            <Select id="fw" value={hours} onChange={(e) => setHours(Number(e.target.value))}>
              <option value={24}>Last 24 hours</option><option value={168}>Last 7 days</option><option value={720}>Last 30 days</option>
            </Select>
          </div>
          {writer ? (
            <Button onClick={() => analyze.mutate()} disabled={analyze.isPending} data-testid="analyze-failures">
              <ScanSearch aria-hidden /> {analyze.isPending ? "Analyzing…" : "Analyze traces"}
            </Button>
          ) : null}
        </div>
      </div>
      {analyze.data ? (
        <p className="text-sm text-muted-foreground" data-testid="analyze-result">
          Detected {analyze.data.detected as number} signals ({analyze.data.new as number} new) in the last {hours} h.
        </p>
      ) : null}
      <InlineError error={analyze.error ?? diagnose.error} />

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Failures over time</CardTitle><CardDescription>Signals per hour by category</CardDescription></CardHeader>
          <CardContent>
            {timeline.rows.length ? <StackedBarChart data={timeline.rows} category="hour" series={timeline.series} />
              : <EmptyState title="No failure signals" description="Run analysis after some workflow or evaluation runs." />}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Suggested investigations</CardTitle><CardDescription>Clusters without a diagnosis yet</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.suggested_investigations.length ? data.suggested_investigations.map((s) => (
              <div key={`${s.agent_id}-${s.category}`} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3 text-sm">
                <div>
                  <p className="font-medium">{s.agent_name as string} · <span className="font-mono">{s.category as string}</span></p>
                  <p className="text-xs text-muted-foreground">{s.reason as string}</p>
                </div>
                {writer ? (
                  <Button size="sm" variant="outline" disabled={diagnose.isPending} data-testid={`diagnose-${s.category}`}
                          onClick={() => diagnose.mutate({ agent_id: s.agent_id as string, category: s.category as string })}>
                    <Stethoscope aria-hidden /> Diagnose
                  </Button>
                ) : null}
              </div>
            )) : <p className="text-sm text-muted-foreground">Nothing to investigate.</p>}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle>Failure clusters</CardTitle><CardDescription>By agent and category; infrastructure is not blamed on the agent</CardDescription></CardHeader>
        <CardContent>
          {data.clusters.length ? (
            <Table data-testid="failure-clusters">
              <THead><tr><TH>Agent</TH><TH>Category</TH><TH>Kind</TH><TH>Signals</TH><TH>Runs</TH><TH>Cost of failed work</TH><TH>Last seen</TH><TH /></tr></THead>
              <TBody>
                {data.clusters.map((c) => (
                  <TR key={`${c.agent_id}-${c.category}-${c.is_infrastructure}`}>
                    <TD>{(c.agent_name as string) ?? "—"}</TD>
                    <TD className="font-mono text-xs">{c.category as string}</TD>
                    <TD>{c.is_infrastructure ? <Badge tone="warning">infrastructure</Badge> : <Badge tone="info">agent</Badge>}</TD>
                    <TD>{c.signals as number}</TD><TD>{c.runs as number}</TD>
                    <TD>{formatCost(c.cost_usd as number)}</TD>
                    <TD className="text-muted-foreground">{relativeTime(c.last_seen as string)}</TD>
                    <TD>{writer && c.agent_id ? (
                      <Button size="sm" variant="ghost" disabled={diagnose.isPending}
                              onClick={() => diagnose.mutate({ agent_id: c.agent_id as string, category: c.category as string })}>Diagnose</Button>
                    ) : null}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          ) : <EmptyState title="No clusters" />}
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Diagnoses</CardTitle><CardDescription>Evidence-linked hypotheses, not verified root causes</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-2">
            {data.diagnoses.length ? data.diagnoses.map((d) => (
              <button key={d.id} onClick={() => setOpen(d.id)} data-testid="diagnosis-row"
                      className="flex flex-col gap-1 rounded-md border p-3 text-left text-sm hover:border-primary/50">
                <span className="flex flex-wrap items-center gap-2">
                  <DecisionBadge value={d.status} /><span className="font-mono text-xs">{d.failure_category}</span>
                  <Badge>confidence: {d.confidence}</Badge>
                  {d.simulated ? <Badge tone="warning">simulated</Badge> : <Badge tone="primary">{d.provider}/{d.model}</Badge>}
                </span>
                <span>{d.hypothesis}</span>
                <span className="text-xs text-muted-foreground">{relativeTime(d.created_at)} · {d.evidence_refs.length} evidence refs</span>
              </button>
            )) : <p className="text-sm text-muted-foreground">No diagnoses yet.</p>}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Most frequent tool failures</CardTitle></CardHeader>
          <CardContent>
            {data.top_tools.length ? (
              <Table><THead><tr><TH>Tool</TH><TH>Status</TH><TH>Count</TH></tr></THead>
                <TBody>{data.top_tools.map((t) => <TR key={`${t.tool}${t.status}`}><TD className="font-mono">{t.tool as string}</TD>
                  <TD><DecisionBadge value={t.status as string} /></TD><TD>{t.count as number}</TD></TR>)}</TBody></Table>
            ) : <p className="text-sm text-muted-foreground">No tool failures.</p>}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle>Signals ({data.signals.length})</CardTitle></CardHeader>
        <CardContent className="max-h-[28rem] overflow-y-auto">
          <Table>
            <THead><tr><TH>When</TH><TH>Category</TH><TH>Detector</TH><TH>Summary</TH><TH>Trace</TH></tr></THead>
            <TBody>
              {data.signals.map((s) => (
                <TR key={s.id}>
                  <TD className="text-muted-foreground">{relativeTime(s.occurred_at)}</TD>
                  <TD className="font-mono text-xs">{s.category}</TD>
                  <TD className="text-xs">{s.detector}</TD>
                  <TD className="max-w-[28rem] truncate text-xs" title={s.summary}>{s.summary}</TD>
                  <TD><Link className="font-mono text-xs text-primary hover:underline" href={`/runs/${s.run_id}`}>{shortId(s.run_id)}</Link></TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>
      <DiagnosisSheet id={open} onClose={() => setOpen(null)} />
    </div>
  );
}

function DiagnosisSheet({ id, onClose }: { id: string | null; onClose: () => void }) {
  const { data } = useDiagnosis(id);
  return (
    <Sheet open={!!id} onOpenChange={(o) => !o && onClose()} title="Diagnosis (hypothesis)"
           description={data ? <span className="font-mono text-xs">{data.failure_category}</span> : null}>
      {data ? (
        <div className="flex flex-col gap-4 text-sm" data-testid="diagnosis-detail">
          <KV items={[
            ["Status", <DecisionBadge key="s" value={data.status} />],
            ["Confidence", data.confidence],
            ["Model", `${data.provider}/${data.model}${data.simulated ? " (simulated)" : ""}`],
          ]} />
          <div><p className="text-xs font-medium text-muted-foreground">Hypothesis</p><p>{data.hypothesis}</p></div>
          <div><p className="text-xs font-medium text-muted-foreground">Proposed remedy</p><p>{data.proposed_remedy}</p></div>
          {data.validation_errors.length ? <p className="text-danger">Rejected: {String(data.validation_errors[0])}</p> : null}
          <div>
            <p className="text-xs font-medium text-muted-foreground">Limitations</p>
            <ul className="list-disc pl-5">{data.limitations.map((l, i) => <li key={i}>{String(l)}</li>)}</ul>
          </div>
          <div className="flex flex-col gap-2">
            <p className="text-xs font-medium text-muted-foreground">Evidence</p>
            {data.evidence.map((e) => (
              <div key={e.signal_id as string} className="rounded-md border p-2 text-xs">
                <p className="flex items-center gap-2">{e.cited ? <Badge tone="primary">cited</Badge> : null}{e.summary as string}</p>
                <Link className="text-primary hover:underline" href={`/runs/${e.run_id}`}>open trace {shortId(e.run_id as string)}</Link>
              </div>
            ))}
          </div>
        </div>
      ) : <LoadingBlock />}
    </Sheet>
  );
}
