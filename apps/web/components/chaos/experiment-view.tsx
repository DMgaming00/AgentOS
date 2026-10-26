"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { Info, OctagonX } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { JsonView, KV } from "@/components/ui/json";
import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useAuth } from "@/lib/auth";
import { useAbortExperiment, useExperiment } from "@/lib/chaos";
import { formatDuration, shortId } from "@/lib/format";
import type { MetricDef, Rate, Scorecard } from "@/types/chaos";

const pct = (r: Rate) => (r.value == null ? "n/a" : `${Math.round(r.value * 1000) / 10}%`);
const ms = (v: number | null | undefined) => (v == null ? "n/a" : formatDuration(v));

function Metric({ id, title, value, sub, def, tone }: { id: string; title: string; value: string; sub: string; def?: MetricDef; tone?: "good" | "bad" | "neutral" }) {
  return (
    <Card className="p-4" data-testid={`metric-${id}`}>
      <p className="flex items-center gap-1 text-xs text-muted-foreground">
        {title}
        {def ? <span title={`${def.definition}\n\nDenominator: ${def.denominator}`} aria-label={`${title}: ${def.definition} Denominator: ${def.denominator}`}><Info className="size-3.5" aria-hidden /></span> : null}
      </p>
      <p className={`mt-1 text-2xl font-semibold ${tone === "bad" ? "text-danger" : tone === "good" ? "text-success" : ""}`}>{value}</p>
      <p className="text-xs text-muted-foreground">{sub}</p>
      {def ? <details className="mt-2 text-xs text-muted-foreground"><summary className="cursor-pointer">Definition</summary><p className="mt-1">{def.definition}</p><p className="mt-1">Denominator: {def.denominator}</p></details> : null}
    </Card>
  );
}

export function Scorecards({ r }: { r: Scorecard }) {
  const m = r.metrics;
  const d = r.definitions;
  const good = (x: Rate) => (x.value == null ? "neutral" : x.value >= 0.999 ? "good" : x.value < 0.5 ? "bad" : "neutral");
  return (
    <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3" data-testid="scorecard">
      <Metric id="completion" title="Task completion rate" value={pct(m.task_completion_rate)} sub={`${m.task_completion_rate.numerator} of ${m.task_completion_rate.denominator} planned runs succeeded`} def={d.task_completion_rate} tone={good(m.task_completion_rate)} />
      <Metric id="exposure" title="Fault exposure (descriptive)" value={pct(m.fault_exposure)} sub={`${m.fault_exposure.numerator} of ${m.fault_exposure.denominator} runs received a fault · ${r.injections_total} injections`} def={d.fault_exposure} />
      <Metric id="recovery" title="Recovery rate" value={pct(m.recovery_rate)} sub={`${m.recovery_rate.numerator} of ${m.recovery_rate.denominator} affected runs still succeeded`} def={d.recovery_rate} tone={good(m.recovery_rate)} />
      <Metric id="amplification" title="Retry amplification (descriptive)" value={m.retry_amplification.attempts_per_step == null ? "n/a" : `${m.retry_amplification.attempts_per_step.toFixed(2)}× attempts/step`}
              sub={`${m.retry_amplification.attempts} attempts over ${m.retry_amplification.steps} steps${m.retry_amplification.extra_attempts_per_injection == null ? "" : ` · ${m.retry_amplification.extra_attempts_per_injection.toFixed(2)} extra per injection`}`} def={d.retry_amplification} />
      <Metric id="detection" title="Time to detection" value={ms(m.time_to_detection_ms.median)} sub={`median of ${m.time_to_detection_ms.n} · p95 ${ms(m.time_to_detection_ms.p95)} · ${m.time_to_detection_ms.silent_faults ?? 0} absorbed silently`} def={d.time_to_detection_ms} />
      <Metric id="ttr" title="Time to recovery" value={ms(m.time_to_recovery_ms.median)} sub={`median of ${m.time_to_recovery_ms.n} · p95 ${ms(m.time_to_recovery_ms.p95)}`} def={d.time_to_recovery_ms} />
      <Metric id="duplicates" title="Duplicate side-effect prevention" value={pct(m.duplicate_side_effect_prevention)} sub={`${m.duplicate_side_effect_prevention.numerator} of ${m.duplicate_side_effect_prevention.denominator} duplicates neutralized · ${m.duplicate_side_effect_prevention.duplicate_successful_executions_found} duplicate executions found`} def={d.duplicate_side_effect_prevention} tone={good(m.duplicate_side_effect_prevention)} />
      <Metric id="consistency" title="State consistency" value={pct(m.state_consistency)} sub={`${m.state_consistency.numerator} of ${m.state_consistency.denominator} invariant checks passed`} def={d.state_consistency} tone={good(m.state_consistency)} />
      <Metric id="budget" title="Budget compliance" value={pct(m.budget_compliance)} sub={`${m.budget_compliance.numerator} of ${m.budget_compliance.denominator} runs within budget`} def={d.budget_compliance} tone={good(m.budget_compliance)} />
      <Metric id="policy" title="Policy held under hostile output" value={pct(m.policy_held_under_hostile_output)} sub={`${m.policy_held_under_hostile_output.numerator} of ${m.policy_held_under_hostile_output.denominator} runs with no unauthorized side effect`} def={d.policy_held_under_hostile_output} />
    </div>
  );
}

export function ExperimentView() {
  const { id } = useParams<{ id: string }>();
  const q = useExperiment(id);
  const abort = useAbortExperiment(id);
  const { role } = useAuth();
  if (q.isLoading) return <LoadingBlock rows={8} />;
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return null;
  const e = q.data;
  const r = e.results;
  const canWrite = role === "admin" || role === "developer";
  return (
    <div className="flex flex-col gap-4" data-testid="experiment-view" data-status={e.status}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs text-muted-foreground"><Link href="/chaos" className="hover:underline">Chaos Lab</Link> / experiment</p>
          <h1 className="flex items-center gap-3 text-xl font-semibold">{e.name} <span className="font-mono text-sm text-muted-foreground">{shortId(e.id)}</span>
            <span data-testid="experiment-status"><StatusBadge status={e.status === "completed" ? "succeeded" : e.status === "running" ? "running" : e.status} /></span>
            {r?.partial ? <Badge tone="warning">partial results</Badge> : null}</h1>
          <p className="text-xs text-muted-foreground">
            seed {e.seed} · {e.trials} runs · faults: {[...new Set(e.faults)].join(", ")} · plan <span className="font-mono">{e.plan_sha256.slice(0, 10)}</span>
            {e.abort_reason ? ` · ${e.abort_reason}` : ""}
          </p>
        </div>
        {canWrite && e.status === "running" ? (
          <Button variant="destructive" size="sm" data-testid="abort-experiment" onClick={() => abort.mutate()} disabled={abort.isPending}><OctagonX aria-hidden /> Abort</Button>
        ) : null}
      </div>
      <InlineError error={abort.error} />
      {e.status === "running" ? <p className="text-sm text-muted-foreground" aria-live="polite">Running… {r ? "" : "results appear when every run is finished or the window closes."}</p> : null}

      {r ? (
        <>
          <Scorecards r={r} />
          <p className="text-xs text-muted-foreground">{r.scoring_note} Costs, where shown, are estimates (simulated pricing offline).</p>
        </>
      ) : null}

      <Card>
        <CardHeader><CardTitle>Conditions</CardTitle><CardDescription>Recorded so the experiment can be reproduced and interpreted.</CardDescription></CardHeader>
        <CardContent><KV items={Object.entries(e.conditions).map(([k, v]) => [k.replace(/_/g, " "), String(v)] as [string, string])} /></CardContent>
      </Card>

      {r ? (
        <Card>
          <CardHeader><CardTitle>Runs</CardTitle><CardDescription>Open any run in the Flight Recorder to replay exactly what happened.</CardDescription></CardHeader>
          <CardContent>
            <Table data-testid="per-case">
              <THead><tr><TH>Case</TH><TH>Run</TH><TH>Status</TH><TH>Injections</TH><TH>Attempts</TH><TH>Error</TH></tr></THead>
              <TBody>{r.per_case.map((c) => (
                <TR key={c.run_id}><TD>{c.case_index}</TD><TD><Link className="font-mono text-primary hover:underline" href={`/flight/${c.run_id}`}>{shortId(c.run_id)}</Link></TD>
                  <TD><StatusBadge status={c.status} /></TD><TD>{c.injections}</TD><TD>{c.attempts}</TD><TD className="text-xs">{c.error ?? "—"}</TD></TR>))}</TBody>
            </Table>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader><CardTitle>Injection log</CardTitle><CardDescription>Append-only evidence: recorded before each fault is applied; the outcome is attached once.</CardDescription></CardHeader>
        <CardContent>
          {e.injections.length ? (
            <Table data-testid="injections">
              <THead><tr><TH>When</TH><TH>Fault</TH><TH>Point</TH><TH>Case · step</TH><TH>Attempt</TH><TH>Outcome</TH></tr></THead>
              <TBody>{e.injections.slice(0, 200).map((i) => (
                <TR key={i.id}><TD className="text-xs text-muted-foreground">{new Date(i.at).toLocaleTimeString()}</TD><TD className="font-mono text-xs">{i.fault_type}</TD><TD className="text-xs">{i.point}</TD>
                  <TD className="text-xs">{i.run_id ? <Link className="text-primary hover:underline" href={`/flight/${i.run_id}`}>#{i.case_index}</Link> : `#${i.case_index}`} {i.step_key ?? ""}</TD>
                  <TD>{i.attempt ?? "—"}</TD><TD className="max-w-80 text-xs">{i.outcome ? <JsonView value={i.outcome} maxHeight="4rem" /> : "—"}</TD></TR>))}</TBody>
            </Table>
          ) : <p className="text-sm text-muted-foreground">No faults have fired yet.</p>}
        </CardContent>
      </Card>
    </div>
  );
}
