"use client";

import { Coins, Radar } from "lucide-react";
import { useState } from "react";

import { HBarChart, SeriesLineChart } from "@/components/charts/charts";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Input, Select } from "@/components/ui/inputs";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/lib/auth";
import { useAnomalies, useBudgets, useFinops, useFinopsMutations, useSuggestions } from "@/lib/finops";
import { useAgents } from "@/lib/queries";
import type { Budget } from "@/types/finops";

const usd = (v: number | null | undefined, digits = 4) => (v == null ? "n/a" : `$${v.toFixed(digits)}`);
const pct = (v: number | null | undefined) => (v == null ? "n/a" : `${(v * 100).toFixed(0)}%`);
const STATE_TONE = { ok: "success", warning: "warning", exceeded: "danger" } as const;
const SEV_TONE = { info: "neutral", warning: "warning", high: "danger" } as const;
const BASIS_TONE = { measured: "success", projected: "warning", none: "neutral" } as const;

function Tile({ id, title, value, sub }: { id: string; title: string; value: string; sub: string }) {
  return (
    <Card className="p-4" data-testid={`fin-${id}`}>
      <p className="text-xs text-muted-foreground">{title}</p>
      <p className="mt-1 text-2xl font-semibold">{value}</p>
      <p className="text-xs text-muted-foreground">{sub}</p>
    </Card>
  );
}

function BudgetsTab({ admin }: { admin: boolean }) {
  const budgets = useBudgets();
  const agents = useAgents();
  const m = useFinopsMutations();
  const [form, setForm] = useState({ scope: "workspace", period: "daily", limit: "5.00", mode: "enforce", agent: "" });
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader><CardTitle>Spending controls</CardTitle><CardDescription>Enforcing budgets stop a model call <em>before</em> it is made, in code. Spend counts recorded usage plus budget reserved by calls in flight. Estimated cost — simulated pricing.</CardDescription></CardHeader>
        <CardContent>
          {budgets.isLoading ? <LoadingBlock rows={2} /> : !budgets.data?.length ? <EmptyState title="No budgets" description={admin ? "Add one below." : "An admin can add budgets."} /> : (
            <Table data-testid="budget-table">
              <THead><tr><TH>Scope</TH><TH>Period</TH><TH>Spent / limit</TH><TH>Utilization</TH><TH>Mode</TH><TH>Projection</TH>{admin ? <TH /> : null}</tr></THead>
              <TBody>{budgets.data.map((b: Budget) => (
                <TR key={b.id} data-state={b.state}>
                  <TD>{b.scope === "workspace" ? "Workspace" : (b.agent ?? "Agent")}</TD><TD>{b.period}</TD>
                  <TD className="text-xs">{usd(b.spent_usd + b.reserved_usd, 6)} / {usd(b.limit_usd, 6)}{b.reserved_usd > 0 ? <span className="text-muted-foreground"> (incl. {usd(b.reserved_usd, 6)} in flight)</span> : null}</TD>
                  <TD><Badge tone={STATE_TONE[b.state]}>{pct(b.utilization)} · {b.state}</Badge></TD>
                  <TD>{b.enabled ? b.mode : "disabled"}</TD>
                  <TD className="text-xs text-muted-foreground" title={b.projection_note}>{b.projected_period_spend_usd == null ? "n/a" : `${usd(b.projected_period_spend_usd, 4)} (projected)`}</TD>
                  {admin ? <TD className="space-x-1 whitespace-nowrap">
                    <Button size="sm" variant="outline" onClick={() => m.patchBudget.mutate({ id: b.id, body: { enabled: !b.enabled } })}>{b.enabled ? "Disable" : "Enable"}</Button>
                    <Button size="sm" variant="outline" onClick={() => m.patchBudget.mutate({ id: b.id, body: { mode: b.mode === "enforce" ? "alert" : "enforce" } })}>{b.mode === "enforce" ? "Alert only" : "Enforce"}</Button>
                    <Button size="sm" variant="outline" onClick={() => m.deleteBudget.mutate(b.id)}>Delete</Button>
                  </TD> : null}
                </TR>))}</TBody>
            </Table>)}
          <InlineError error={m.patchBudget.error ?? m.deleteBudget.error} />
        </CardContent>
      </Card>
      {admin ? (
        <Card>
          <CardHeader><CardTitle>New budget</CardTitle></CardHeader>
          <CardContent className="flex flex-wrap items-end gap-2">
            <Select aria-label="Budget scope" className="w-36" value={form.scope} onChange={(e) => setForm({ ...form, scope: e.target.value })}><option value="workspace">Workspace</option><option value="agent">One agent</option></Select>
            {form.scope === "agent" ? <Select aria-label="Budget agent" className="w-56" value={form.agent} onChange={(e) => setForm({ ...form, agent: e.target.value })}><option value="">Choose an agent…</option>{agents.data?.items.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</Select> : null}
            <Select aria-label="Budget period" className="w-32" value={form.period} onChange={(e) => setForm({ ...form, period: e.target.value })}><option value="daily">Daily</option><option value="monthly">Monthly</option></Select>
            <Input aria-label="Budget limit USD" className="w-32" value={form.limit} onChange={(e) => setForm({ ...form, limit: e.target.value })} />
            <Select aria-label="Budget mode" className="w-32" value={form.mode} onChange={(e) => setForm({ ...form, mode: e.target.value })}><option value="enforce">Enforce</option><option value="alert">Alert only</option></Select>
            <Button data-testid="create-budget" disabled={m.createBudget.isPending || (form.scope === "agent" && !form.agent)} onClick={() => m.createBudget.mutate({ scope: form.scope as "workspace" | "agent", period: form.period as "daily" | "monthly", limit_usd: form.limit, mode: form.mode as "enforce" | "alert", agent_id: form.agent || undefined })}>Add budget</Button>
            <InlineError error={m.createBudget.error} />
          </CardContent>
        </Card>) : null}
    </div>
  );
}

function AnomaliesTab({ canWrite }: { canWrite: boolean }) {
  const anomalies = useAnomalies();
  const m = useFinopsMutations();
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <div><CardTitle>Anomalies</CardTitle><CardDescription>Deterministic detectors with fixed thresholds and minimum sample sizes; each shows the numbers that triggered it.</CardDescription></div>
        {canWrite ? <Button data-testid="detect-anomalies" variant="outline" disabled={m.detect.isPending} onClick={() => m.detect.mutate()}><Radar aria-hidden /> Run detection</Button> : null}
      </CardHeader>
      <CardContent className="flex flex-col gap-2">
        {m.detect.data ? <p className="text-xs text-muted-foreground" data-testid="detect-result">Detection found {m.detect.data.found} anomalies ({m.detect.data.new} new).</p> : null}
        {anomalies.isLoading ? <LoadingBlock rows={2} /> : !anomalies.data?.items.length ? <p className="text-sm text-muted-foreground" data-testid="no-anomalies">No anomalies detected. Detectors need history and volume before they fire, so a quiet workspace stays quiet.</p> : anomalies.data.items.map((a) => (
          <div key={a.id} className="rounded-md border p-3 text-sm" data-testid="anomaly">
            <div className="flex items-center gap-2"><Badge tone={SEV_TONE[a.severity]}>{a.severity}</Badge><span className="font-medium">{a.kind.replace(/_/g, " ")}</span><span className="text-xs text-muted-foreground">{a.subject}</span>
              {canWrite && a.status === "open" ? <Button size="sm" variant="outline" className="ml-auto" onClick={() => m.acknowledge.mutate(a.id)}>Acknowledge</Button> : <Badge className="ml-auto">{a.status}</Badge>}</div>
            <p className="mt-1">{a.summary}</p>
            <p className="font-mono text-[11px] text-muted-foreground">{JSON.stringify(a.evidence)}</p>
          </div>))}
      </CardContent>
    </Card>
  );
}

function SuggestionsTab({ days }: { days: number }) {
  const s = useSuggestions(days);
  return (
    <Card>
      <CardHeader><CardTitle>Optimization suggestions</CardTitle><CardDescription>Evidence first. A saving appears only as <strong>measured</strong> (actually spent on something avoidable) or <strong>projected</strong> (with its formula); otherwise no cost claim is made. Nothing is applied automatically.</CardDescription></CardHeader>
      <CardContent className="flex flex-col gap-2">
        {s.isLoading ? <LoadingBlock rows={2} /> : !s.data?.items.length ? <p className="text-sm text-muted-foreground" data-testid="no-suggestions">No suggestion has enough evidence in this window.</p> : s.data.items.map((i) => (
          <div key={i.id} className="rounded-md border p-3 text-sm" data-testid="suggestion">
            <div className="flex items-center gap-2"><span className="font-medium">{i.title}</span><Badge tone={BASIS_TONE[i.savings.basis]}>{i.savings.basis === "none" ? "no cost claim" : `${i.savings.basis}${i.savings.usd != null ? ` · ${usd(i.savings.usd)}` : ""}`}</Badge></div>
            <p className="mt-1 text-muted-foreground">{i.action}</p>
            <p className="text-xs text-muted-foreground">{i.savings.note}</p>
            <p className="font-mono text-[11px] text-muted-foreground">{JSON.stringify(i.evidence)}</p>
          </div>))}
      </CardContent>
    </Card>
  );
}

export function FinopsPage() {
  const { role } = useAuth();
  const [days, setDays] = useState(30);
  const sum = useFinops(days);
  const canWrite = role === "admin" || role === "developer";
  if (sum.isLoading) return <LoadingBlock rows={6} />;
  if (sum.error) return <ErrorState error={sum.error} onRetry={() => void sum.refetch()} />;
  const s = sum.data!;
  const t = s.totals;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold"><Coins className="size-5" aria-hidden /> FinOps</h1>
          <p className="text-sm text-muted-foreground">Where the estimated spend goes, what is wasted, what is about to run out, and what is worth changing.</p>
        </div>
        <Select aria-label="Window" className="w-36" value={days} onChange={(e) => setDays(Number(e.target.value))}><option value={7}>Last 7 days</option><option value={30}>Last 30 days</option><option value={90}>Last 90 days</option></Select>
      </div>
      <div role="note" className="rounded-lg border bg-card p-3 text-sm" data-testid="estimate-label">
        <Badge tone="warning" className="whitespace-normal break-words text-left">{s.label}</Badge> <span className="text-muted-foreground">Computed from reported token usage and the versioned price table {s.pricing.version} ({pct(t.simulated_share)} of calls simulated). Never an invoice.{t.calls_with_unknown_cost ? ` ${t.calls_with_unknown_cost} calls have unknown cost and are excluded, not counted as zero.` : ""}</span>
      </div>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" data-testid="finops-tiles">
        <Tile id="total" title="Estimated spend" value={usd(t.estimated_cost_usd)} sub={`${t.calls} model calls in ${s.window_days} days`} />
        <Tile id="retry" title="Spent on failed attempts" value={usd(t.retry_cost_usd)} sub={`${pct(t.retry_cost_share)} of spend`} />
        <Tile id="failed-runs" title="Spent on failed runs" value={usd(t.failed_run_cost_usd)} sub={`${pct(t.failed_run_cost_share)} of spend`} />
        <Tile id="trend" title="Last 7 vs previous 7 days" value={s.trend ? (s.trend.change == null ? "n/a" : `${s.trend.change >= 0 ? "+" : ""}${(s.trend.change * 100).toFixed(0)}%`) : "n/a"} sub={s.trend ? `${usd(s.trend.last_7_days_usd)} vs ${usd(s.trend.previous_7_days_usd)}` : "needs a 14+ day window"} />
      </div>
      <Tabs defaultValue="usage">
        <TabsList><TabsTrigger value="usage">Usage</TabsTrigger><TabsTrigger value="budgets">Budgets</TabsTrigger><TabsTrigger value="anomalies">Anomalies</TabsTrigger><TabsTrigger value="suggestions">Suggestions</TabsTrigger></TabsList>
        <TabsContent value="usage">
          <div className="grid gap-4 xl:grid-cols-2">
            <Card><CardHeader><CardTitle>Daily estimated cost</CardTitle></CardHeader><CardContent>
              <SeriesLineChart data={s.daily.map((d) => ({ day: d.day.slice(5), cost: d.estimated_cost_usd }))} x="day" series={[{ key: "cost", label: "Estimated cost (USD)", color: "var(--series-1)" }]} format={(v) => `$${v.toFixed(4)}`} />
            </CardContent></Card>
            <Card><CardHeader><CardTitle>Cost by agent</CardTitle></CardHeader><CardContent>
              {s.by_agent.length ? <HBarChart data={s.by_agent.slice(0, 8).map((a) => ({ agent: a.agent, cost: a.estimated_cost_usd ?? 0 }))} category="agent" value="cost" valueLabel="Estimated cost (USD)" format={(v) => `$${v.toFixed(4)}`} /> : <EmptyState title="No usage yet" description="Run a workflow to see cost." />}
            </CardContent></Card>
          </div>
          <Card className="mt-4"><CardHeader><CardTitle>By agent</CardTitle></CardHeader><CardContent>
            <Table data-testid="agent-cost">
              <THead><tr><TH>Agent</TH><TH>Cost</TH><TH>Share</TH><TH>Cost / successful step</TH><TH>Failed-attempt cost</TH><TH>Failed-run cost</TH><TH>Steps ok / failed</TH></tr></THead>
              <TBody>{s.by_agent.map((a) => <TR key={a.agent}><TD>{a.agent}</TD><TD>{usd(a.estimated_cost_usd)}</TD><TD>{pct(a.share)}</TD><TD>{a.cost_per_successful_step_usd == null ? <span title="undefined: no successful steps">n/a</span> : usd(a.cost_per_successful_step_usd, 6)}</TD><TD>{usd(a.retry_cost_usd)}</TD><TD>{usd(a.failed_run_cost_usd)}</TD><TD>{a.successful_steps} / {a.failed_steps}</TD></TR>)}</TBody>
            </Table>
          </CardContent></Card>
          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <Card><CardHeader><CardTitle>By workflow</CardTitle></CardHeader><CardContent>
              <Table><THead><tr><TH>Workflow</TH><TH>Runs ok / total</TH><TH>Cost</TH><TH>Cost / successful run</TH></tr></THead><TBody>{s.by_workflow.slice(0, 10).map((w) => <TR key={w.workflow}><TD className="max-w-48 truncate">{w.workflow}</TD><TD>{w.succeeded_runs} / {w.runs}</TD><TD>{usd(w.estimated_cost_usd)}</TD><TD>{usd(w.cost_per_succeeded_run_usd, 6)}</TD></TR>)}</TBody></Table>
            </CardContent></Card>
            <Card><CardHeader><CardTitle>By model and tool</CardTitle></CardHeader><CardContent className="flex flex-col gap-3">
              <Table><THead><tr><TH>Model</TH><TH>Calls</TH><TH>Tokens in / out</TH><TH>Cost</TH><TH>p95</TH></tr></THead><TBody>{s.by_model.map((m) => <TR key={`${m.provider}/${m.model}`}><TD className="text-xs">{m.provider}/{m.model}</TD><TD>{m.calls}</TD><TD className="text-xs">{m.input_tokens} / {m.output_tokens}</TD><TD>{usd(m.estimated_cost_usd)}</TD><TD>{m.latency_p95_ms == null ? "n/a" : `${Math.round(m.latency_p95_ms)} ms`}</TD></TR>)}</TBody></Table>
              <Table><THead><tr><TH>Tool</TH><TH>Calls</TH><TH>Failed</TH><TH>Denied</TH></tr></THead><TBody>{s.tools.map((x) => <TR key={x.tool}><TD>{x.tool}</TD><TD>{x.calls}</TD><TD>{x.failed}</TD><TD>{x.denied}</TD></TR>)}</TBody></Table>
              <p className="text-xs text-muted-foreground">Tool calls carry no model cost of their own here; counts show where the work goes.</p>
            </CardContent></Card>
          </div>
        </TabsContent>
        <TabsContent value="budgets"><BudgetsTab admin={role === "admin"} /></TabsContent>
        <TabsContent value="anomalies"><AnomaliesTab canWrite={canWrite} /></TabsContent>
        <TabsContent value="suggestions"><SuggestionsTab days={days} /></TabsContent>
      </Tabs>
    </div>
  );
}
