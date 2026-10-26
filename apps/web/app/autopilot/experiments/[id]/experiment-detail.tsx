"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Rocket, XCircle } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useState } from "react";

import { GroupedBarChart, HBarChart } from "@/components/charts/charts";
import { CIChart } from "@/components/autopilot/ci-chart";
import { DecisionBadge } from "@/components/autopilot/decision";
import { DiffView } from "@/components/autopilot/diff-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Input, Select } from "@/components/ui/inputs";
import { StatCard } from "@/components/ui/stat";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { pct, rate, useExperiment } from "@/lib/autopilot";
import { canWrite, useAuth } from "@/lib/auth";
import { formatCost, formatDuration } from "@/lib/format";
import type { EvalMetrics, Gate } from "@/types/autopilot";

type Arm = { version: number; metrics: EvalMetrics; conditions: Record<string, unknown>; evaluation_run_id: string; latencies_ms: number[] };
type Results = {
  baseline: Arm; candidate: Arm;
  comparison: { n_matched: number; diff_mean: number | null; ci95: [number, number] | null; mcnemar_p: number | null; significant: boolean;
    interpretation: string; regressions: string[]; improvements: string[]; settings: Record<string, unknown>;
    per_case: { case_id: string; baseline: number; candidate: number; diff: number; baseline_failing: string[]; candidate_failing: string[]; tags: string[] }[] };
  cost: { baseline_per_success: number | null; candidate_per_success: number | null; baseline_total: number | null; candidate_total: number | null;
    per_success_change: number | null; total_change: number | null };
  error_distribution: { baseline: Record<string, number> | null; candidate: Record<string, number> | null };
  diff: { field: string; before?: unknown; after?: unknown }[]; denied_tool_calls: number; notes: string[];
};
const BASE = { color: "var(--series-1)" }, CAND = { color: "var(--series-2)" };

export function ExperimentDetail() {
  const { id } = useParams<{ id: string }>();
  const { data: e, error, isLoading } = useExperiment(id);
  if (isLoading) return <LoadingBlock rows={8} />;
  if (error || !e) return <ErrorState error={error} />;
  const r = e.results as unknown as Results | null;
  const elig = e.eligibility as { decision: string; summary: string; improvement_demonstrated: boolean; gates: Gate[] } | null;
  const cond = e.conditions as { dataset?: { name: string; split: string; version: number; hash: string; cases: number } };
  return (
    <div className="flex flex-col gap-4" data-testid="experiment-detail">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs text-muted-foreground"><Link href="/autopilot/experiments" className="hover:underline">Experiments</Link></p>
          <h2 className="flex flex-wrap items-center gap-2 text-lg font-semibold">
            {e.agent_name}: v{e.baseline_version} vs v{e.candidate_version}
            {e.status === "running" ? <DecisionBadge value="running" /> : <span data-testid="experiment-decision"><DecisionBadge value={e.decision} /></span>}
          </h2>
          <p className="text-sm text-muted-foreground">
            {e.candidate_title ?? "manual comparison"} · {cond.dataset?.name} {cond.dataset?.split} v{cond.dataset?.version} ({cond.dataset?.cases} cases) · {e.trials} trial(s)
          </p>
        </div>
        {e.decision === "eligible" ? <PromoteBox experimentId={e.id} /> : null}
      </div>
      {e.status === "running" ? <LoadingBlock rows={3} label="Experiment running" /> : null}
      {elig && r ? (
        <>
          <Card className={elig.decision === "eligible" ? "border-success/40" : elig.decision === "not_eligible" ? "border-danger/40" : "border-warning/40"}>
            <CardContent className="flex flex-col gap-1 pt-4 text-sm">
              <p className="font-medium" data-testid="experiment-summary">{elig.summary}</p>
              <p className="text-muted-foreground">{r.comparison.interpretation}</p>
              <p className="text-xs text-muted-foreground">Exact McNemar p = {r.comparison.mcnemar_p == null ? "n/a (no discordant pairs)" : r.comparison.mcnemar_p.toFixed(4)} · {r.notes.join(" ")}</p>
            </CardContent>
          </Card>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <StatCard label="Task success" value={`${pct(r.baseline.metrics.task_success_rate.value)} → ${pct(r.candidate.metrics.task_success_rate.value)}`}
                      hint={`${rate(r.baseline.metrics.task_success_rate)} vs ${rate(r.candidate.metrics.task_success_rate)}`} />
            <StatCard label="Cost per successful task" value={`${formatCost(r.cost.baseline_per_success)} → ${formatCost(r.cost.candidate_per_success)}`}
                      hint={`total ${formatCost(r.cost.baseline_total)} → ${formatCost(r.cost.candidate_total)}${r.cost.candidate_per_success == null ? " · undefined: no successes" : ""}`} />
            <StatCard label="p95 latency" value={`${formatDuration(r.baseline.metrics.latency_ms.p95)} → ${formatDuration(r.candidate.metrics.latency_ms.p95)}`} />
            <StatCard label="Regressions" value={r.comparison.regressions.length} tone={r.comparison.regressions.length ? "danger" : undefined}
                      hint={`${r.comparison.improvements.length} improvements · ${r.denied_tool_calls} denied tool calls`} />
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <CardHeader><CardTitle>Quality: baseline vs candidate</CardTitle><CardDescription>Share of executions passing each check</CardDescription></CardHeader>
              <CardContent>
                <GroupedBarChart category="metric" format={(v) => `${(v * 100).toFixed(0)}%`} domain={[0, 1]}
                  series={[{ key: "baseline", label: `Baseline v${r.baseline.version}`, ...BASE }, { key: "candidate", label: `Candidate v${r.candidate.version}`, ...CAND }]}
                  data={([["Task success", "task_success_rate"], ["Schema", "schema_compliance"], ["Numeric", "numeric_accuracy"],
                          ["Grounding", "grounding_pass_rate"], ["Tool use", "tool_call_correctness"]] as const).map(([label, k]) => ({
                    metric: label, baseline: r.baseline.metrics[k]?.value ?? null, candidate: r.candidate.metrics[k]?.value ?? null }))} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>Paired success difference</CardTitle><CardDescription>Gate decisions use this interval, not point estimates</CardDescription></CardHeader>
              <CardContent><CIChart diff={r.comparison.diff_mean} ci={r.comparison.ci95} n={r.comparison.n_matched} /></CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>Cost per successful task</CardTitle><CardDescription>Estimated from usage; a cheaper candidate that fails more is not better</CardDescription></CardHeader>
              <CardContent>
                <GroupedBarChart category="arm" format={(v) => formatCost(v)} series={[{ key: "cost", label: "USD per success", ...BASE }]}
                  data={[{ arm: `Baseline v${r.baseline.version}`, cost: r.cost.baseline_per_success }, { arm: `Candidate v${r.candidate.version}`, cost: r.cost.candidate_per_success }]} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>Latency</CardTitle><CardDescription>Per execution, ms</CardDescription></CardHeader>
              <CardContent>
                <GroupedBarChart category="stat" format={(v) => formatDuration(v)}
                  series={[{ key: "baseline", label: "Baseline", ...BASE }, { key: "candidate", label: "Candidate", ...CAND }]}
                  data={(["mean", "p50", "p95"] as const).map((s) => ({ stat: s, baseline: r.baseline.metrics.latency_ms[s], candidate: r.candidate.metrics.latency_ms[s] }))} />
              </CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>Failure breakdown (candidate)</CardTitle></CardHeader>
              <CardContent>
                {Object.keys(r.error_distribution.candidate ?? {}).length ? (
                  <HBarChart category="category" value="count" valueLabel="Failed executions" color="var(--series-2)"
                    data={Object.entries(r.error_distribution.candidate ?? {}).map(([category, count]) => ({ category, count }))} />
                ) : <p className="text-sm text-muted-foreground">No failures.</p>}
              </CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>Failure breakdown (baseline)</CardTitle></CardHeader>
              <CardContent>
                {Object.keys(r.error_distribution.baseline ?? {}).length ? (
                  <HBarChart category="category" value="count" valueLabel="Failed executions" color="var(--series-1)"
                    data={Object.entries(r.error_distribution.baseline ?? {}).map(([category, count]) => ({ category, count }))} />
                ) : <p className="text-sm text-muted-foreground">No failures.</p>}
              </CardContent>
            </Card>
          </div>
          <Card>
            <CardHeader><CardTitle>Deployment gates</CardTitle></CardHeader>
            <CardContent>
              <Table data-testid="gates">
                <THead><tr><TH>Gate</TH><TH>Severity</TH><TH>Result</TH><TH>Detail</TH></tr></THead>
                <TBody>{elig.gates.map((g) => (
                  <TR key={g.gate}><TD className="font-mono text-xs">{g.gate}</TD><TD><Badge>{g.severity}</Badge></TD>
                    <TD>{g.passed ? <span className="flex items-center gap-1 text-success"><CheckCircle2 className="size-4" aria-hidden />pass</span>
                      : <span className="flex items-center gap-1 text-danger"><XCircle className="size-4" aria-hidden />fail</span>}</TD>
                    <TD className="text-xs">{g.detail}</TD></TR>))}</TBody>
              </Table>
            </CardContent>
          </Card>
          <div className="grid gap-4 xl:grid-cols-2">
            <Card>
              <CardHeader><CardTitle>Candidate configuration diff</CardTitle></CardHeader>
              <CardContent><DiffView diff={r.diff} /></CardContent>
            </Card>
            <Card>
              <CardHeader><CardTitle>Conditions</CardTitle><CardDescription>Recorded for reproducibility</CardDescription></CardHeader>
              <CardContent className="grid gap-2 text-xs md:grid-cols-2">
                {(["baseline", "candidate"] as const).map((arm) => {
                  const c = r[arm].conditions as Record<string, unknown>;
                  return (
                    <dl key={arm} className="rounded-md border p-2">
                      <dt className="mb-1 font-medium capitalize">{arm} v{String(c.agent_version)}</dt>
                      {["provider", "model", "config_hash", "prompt_hash", "max_steps", "effort", "temperature", "seed", "started_at", "completed_at"].map((k) => (
                        <div key={k} className="flex justify-between gap-2"><span className="text-muted-foreground">{k}</span><span className="truncate font-mono">{String(c[k] ?? "—")}</span></div>
                      ))}
                    </dl>);
                })}
              </CardContent>
            </Card>
          </div>
          <Card>
            <CardHeader><CardTitle>Per-case results</CardTitle><CardDescription>Regressions highlighted; scores are mean success over trials</CardDescription></CardHeader>
            <CardContent className="max-h-[32rem] overflow-y-auto">
              <Table data-testid="per-case">
                <THead><tr><TH>Case</TH><TH>Tags</TH><TH>Baseline</TH><TH>Candidate</TH><TH>Baseline failing</TH><TH>Candidate failing</TH></tr></THead>
                <TBody>{r.comparison.per_case.map((c) => (
                  <TR key={c.case_id} className={c.diff < 0 ? "bg-danger/10" : c.diff > 0 ? "bg-success/5" : undefined} data-regression={String(c.diff < 0)}>
                    <TD className="font-mono text-xs">{c.case_id}</TD><TD className="text-xs">{c.tags.join(", ")}</TD>
                    <TD>{pct(c.baseline, 0)}</TD><TD>{pct(c.candidate, 0)}</TD>
                    <TD className="text-xs">{c.baseline_failing.join(", ") || "—"}</TD><TD className="text-xs">{c.candidate_failing.join(", ") || "—"}</TD>
                  </TR>))}</TBody>
              </Table>
            </CardContent>
          </Card>
        </>
      ) : null}
    </div>
  );
}

function PromoteBox({ experimentId }: { experimentId: string }) {
  const { role } = useAuth();
  const qc = useQueryClient();
  const [mode, setMode] = useState<"full" | "canary">("full");
  const [percent, setPercent] = useState(20);
  const promote = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/deployments/promote", {
      params: { header: { "Idempotency-Key": idempotencyKey() } },
      body: { experiment_id: experimentId, mode, canary_percent: mode === "canary" ? percent : 0, reason: "Eligible in controlled experiment" } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["autopilot"] }),
  });
  if (!canWrite(role)) return null;
  return (
    <div className="flex flex-col items-end gap-2">
      <div className="flex items-center gap-2">
        <label htmlFor="pm" className="sr-only">Rollout mode</label>
        <Select id="pm" className="w-32" value={mode} onChange={(e) => setMode(e.target.value as "full" | "canary")}>
          <option value="full">Full rollout</option><option value="canary">Canary</option>
        </Select>
        {mode === "canary" ? <Input aria-label="Canary percent" type="number" className="w-20" min={1} max={99} value={percent} onChange={(e) => setPercent(Number(e.target.value))} /> : null}
        <Button onClick={() => promote.mutate()} disabled={promote.isPending || promote.isSuccess} data-testid="request-promotion">
          <Rocket aria-hidden /> {promote.isSuccess ? "Approval requested" : "Request promotion"}
        </Button>
      </div>
      {promote.isSuccess ? <Link href="/autopilot/deployments" className="text-xs text-primary hover:underline">An administrator must approve it on Deployments →</Link> : null}
      <InlineError error={promote.error} />
    </div>
  );
}
