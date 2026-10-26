"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useChaosCompare } from "@/lib/chaos";
import { shortId } from "@/lib/format";
import type { Rate } from "@/types/chaos";

const pct = (v: number | null | undefined) => (v == null ? "n/a" : `${Math.round(v * 1000) / 10}%`);

export function ChaosCompareView() {
  const sp = useSearchParams();
  const a = sp.get("a"), b = sp.get("b");
  const q = useChaosCompare(a, b);
  if (!a || !b) return <EmptyState title="Pick two experiments" description="Select two completed experiments with the same plan on the Chaos Lab page." />;
  if (q.isLoading) return <LoadingBlock rows={6} />;
  if (q.error) return <ErrorState error={q.error} />;
  if (!q.data) return null;
  const c = q.data;
  const p = c.paired_completion;
  const rows: [string, string][] = [["task_completion_rate", "Task completion rate"], ["recovery_rate", "Recovery rate"], ["state_consistency", "State consistency"], ["budget_compliance", "Budget compliance"]];
  return (
    <div className="flex flex-col gap-4" data-testid="chaos-compare">
      <div>
        <h1 className="text-xl font-semibold">Reliability before / after</h1>
        <p className="text-sm text-muted-foreground">
          <Link className="font-mono text-primary hover:underline" href={`/chaos/${a}`}>baseline {shortId(a)}</Link> vs{" "}
          <Link className="font-mono text-primary hover:underline" href={`/chaos/${b}`}>candidate {shortId(b)}</Link> · same plan <span className="font-mono">{c.same_fault_schedule.plan_sha256.slice(0, 10)}</span>, seed {c.same_fault_schedule.seed}, {c.same_fault_schedule.trials} runs
        </p>
      </div>
      <Card data-testid="paired-verdict">
        <CardHeader><CardTitle>Paired completion (OpenEval statistics)</CardTitle><CardDescription>{p.interpretation}</CardDescription></CardHeader>
        <CardContent className="grid gap-3 text-sm md:grid-cols-5">
          <div><p className="text-xs text-muted-foreground">Baseline</p><p className="text-xl font-semibold">{pct(p.baseline_success)}</p></div>
          <div><p className="text-xs text-muted-foreground">Candidate</p><p className="text-xl font-semibold">{pct(p.candidate_success)}</p></div>
          <div><p className="text-xs text-muted-foreground">Paired difference</p><p className="text-xl font-semibold">{p.diff_mean == null ? "n/a" : `${p.diff_mean >= 0 ? "+" : ""}${p.diff_mean.toFixed(3)}`}</p><p className="text-xs text-muted-foreground">95% CI {p.ci95 ? `[${p.ci95[0].toFixed(3)}, ${p.ci95[1].toFixed(3)}]` : "n/a"}, n={p.n_matched}</p></div>
          <div><p className="text-xs text-muted-foreground">McNemar p</p><p className="text-xl font-semibold">{p.mcnemar_p == null ? "n/a" : p.mcnemar_p.toFixed(4)}</p></div>
          <div><p className="text-xs text-muted-foreground">Verdict</p>{p.significant ? <Badge tone="success">statistically supported</Badge> : <Badge tone="warning">inconclusive</Badge>}</div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Metrics</CardTitle><CardDescription>{c.caution}</CardDescription></CardHeader>
        <CardContent>
          <Table>
            <THead><tr><TH>Metric</TH><TH>Baseline</TH><TH>Candidate</TH><TH>Change</TH></tr></THead>
            <TBody>
              {rows.map(([k, label]) => {
                const d = c.deltas[k] as { baseline: Rate; candidate: Rate; delta: number | null };
                return <TR key={k}><TD>{label}</TD><TD>{pct(d.baseline.value)} <span className="text-xs text-muted-foreground">({d.baseline.numerator}/{d.baseline.denominator})</span></TD>
                  <TD>{pct(d.candidate.value)} <span className="text-xs text-muted-foreground">({d.candidate.numerator}/{d.candidate.denominator})</span></TD><TD>{d.delta == null ? "n/a" : `${d.delta >= 0 ? "+" : ""}${(d.delta * 100).toFixed(1)} pts`}</TD></TR>;
              })}
              <TR><TD>Retry amplification</TD><TD>{(c.deltas.retry_amplification.baseline as { attempts_per_step: number | null }).attempts_per_step?.toFixed(2) ?? "n/a"}</TD><TD>{(c.deltas.retry_amplification.candidate as { attempts_per_step: number | null }).attempts_per_step?.toFixed(2) ?? "n/a"}</TD><TD className="text-xs text-muted-foreground">attempts per step (descriptive)</TD></TR>
            </TBody>
          </Table>
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Per case</CardTitle><CardDescription>Regressions: {p.regressions.length} · improvements: {p.improvements.length}</CardDescription></CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-1" data-testid="case-matrix">
            {p.per_case.map((x) => (
              <span key={x.case_id} title={`case ${x.case_id}: baseline ${x.baseline}, candidate ${x.candidate}`}
                    className={`inline-flex size-8 items-center justify-center rounded border text-xs ${x.candidate > x.baseline ? "border-success text-success" : x.candidate < x.baseline ? "border-danger text-danger" : "text-muted-foreground"}`}>{x.case_id}</span>
            ))}
          </div>
          <p className="mt-2 text-xs text-muted-foreground">green = candidate completed where the baseline did not; red = the reverse; grey = same outcome.</p>
        </CardContent>
      </Card>
    </div>
  );
}
