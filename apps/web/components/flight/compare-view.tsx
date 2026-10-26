"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useRunDiff } from "@/lib/flight";
import { formatCost, formatDuration, shortId } from "@/lib/format";
import type { StepDiff } from "@/types/flight";

const KIND_LABEL: Record<string, string> = {
  branch: "different branch", status: "different status", retries: "different retries", output: "different output",
  model_response: "different model response", tool_calls: "different tool calls", presence: "only in one run",
};

export function CompareView() {
  const sp = useSearchParams();
  const a = sp.get("a"), b = sp.get("b");
  const q = useRunDiff(a, b);
  if (!a || !b) return <EmptyState title="Pick two runs" description="Choose two runs in the Flight Recorder list." />;
  if (q.isLoading) return <LoadingBlock rows={8} label="Comparing recordings" />;
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return null;
  const { summary: s, steps } = q.data;
  const pair = <T,>(x: [T, T] | undefined, fmt: (v: T) => React.ReactNode = (v) => String(v)) => (x ? <span className="flex gap-2"><span>{fmt(x[0])}</span><span className="text-muted-foreground">→</span><span>{fmt(x[1])}</span></span> : "—");
  return (
    <div className="flex flex-col gap-4" data-testid="run-compare">
      <div>
        <h1 className="text-xl font-semibold">Run comparison</h1>
        <p className="text-sm text-muted-foreground">
          <Link className="font-mono text-primary hover:underline" href={`/flight/${a}`}>A {shortId(a)}</Link> vs{" "}
          <Link className="font-mono text-primary hover:underline" href={`/flight/${b}`}>B {shortId(b)}</Link> · structured step-by-step diff, not raw JSON
        </p>
      </div>
      <Card>
        <CardContent className="grid gap-3 pt-4 text-sm md:grid-cols-3 lg:grid-cols-6" data-testid="compare-summary">
          <Stat label="Verdict">{s.identical ? <Badge tone="success">identical</Badge> : <Badge tone="warning">{s.steps_different} of {s.steps_total} steps differ</Badge>}</Stat>
          <Stat label="Final status">{pair(s.final_status, (v) => <StatusBadge status={v} />)}</Stat>
          <Stat label="Final output">{s.final_output_same ? "same" : `${s.final_output_diff.length} path(s) differ`}</Stat>
          <Stat label="Duration">{pair(s.duration_ms, (v) => formatDuration(v))}</Stat>
          <Stat label="Est. cost (simulated pricing)">{pair(s.cost_usd_estimated_simulated, (v) => formatCost(v))}</Stat>
          <Stat label="Model / tool calls">{pair(s.model_calls)} / {pair(s.tool_calls)}</Stat>
          {s.evaluation ? <Stat label="Evaluation verdict">{pair(s.evaluation.passed, (v) => (v ? "pass" : "fail"))}</Stat> : null}
          {s.branch_differences.length ? <Stat label="Branch differences"><span className="font-mono">{s.branch_differences.join(", ")}</span></Stat> : null}
        </CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Steps</CardTitle><CardDescription>Aligned by node key. Rows with differences are highlighted.</CardDescription></CardHeader>
        <CardContent>
          <Table data-testid="compare-steps">
            <THead><tr><TH>Step</TH><TH>Status A → B</TH><TH>Attempts</TH><TH>Duration</TH><TH>Differences</TH></tr></THead>
            <TBody>
              {steps.map((st: StepDiff) => (
                <TR key={st.key} data-different={st.differences.length > 0} className={st.differences.length ? "bg-warning/5" : undefined}>
                  <TD className="font-mono">{st.key}</TD>
                  <TD>{st.status ? pair(st.status, (v) => <StatusBadge status={v} />) : <Badge>{st.presence}</Badge>}</TD>
                  <TD>{pair(st.attempts)}</TD>
                  <TD>{pair(st.duration_ms, (v) => formatDuration(v))}</TD>
                  <TD>
                    <div className="flex flex-col gap-1">
                      {st.differences.length === 0 ? <span className="text-muted-foreground">same</span> : st.differences.map((d, i) => (
                        <div key={i} className="text-xs"><Badge tone="warning">{KIND_LABEL[d.kind] ?? d.kind}</Badge>
                          {d.kind === "tool_calls" ? <span className="ml-2 font-mono">{(d.a as string[]).join(", ") || "—"} → {(d.b as string[]).join(", ") || "—"}</span> : null}
                          {d.kind === "output" ? <span className="ml-2 font-mono">{(d.paths as { path: string }[]).map((p) => p.path).join(", ")}</span> : null}
                          {d.kind === "model_response" ? <span className="ml-2">call #{(d.calls as number[]).join(", #")}</span> : null}
                        </div>
                      ))}
                      {st.model_calls?.filter((c) => !c.response_same).map((c) => (
                        <div key={c.index} className="grid grid-cols-2 gap-2 rounded border p-2 text-xs">
                          <div><div className="text-muted-foreground">A</div>{c.a?.text ?? "—"}</div>
                          <div><div className="text-muted-foreground">B</div>{c.b?.text ?? "—"}</div>
                        </div>
                      ))}
                    </div>
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}

function Stat({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><p className="text-xs text-muted-foreground">{label}</p><div className="mt-1 font-medium">{children}</div></div>;
}
