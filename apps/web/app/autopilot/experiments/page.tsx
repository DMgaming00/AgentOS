"use client";

import Link from "next/link";

import { DecisionBadge } from "@/components/autopilot/decision";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { pct, useExperiments } from "@/lib/autopilot";
import { formatCost, relativeTime, shortId } from "@/lib/format";

type R = { baseline?: { metrics?: { task_success_rate?: { value: number | null } } }; candidate?: { metrics?: { task_success_rate?: { value: number | null } } };
  cost?: { baseline_per_success?: number | null; candidate_per_success?: number | null }; comparison?: { regressions?: string[]; n_matched?: number } };

export default function ExperimentsPage() {
  const { data, error, isLoading } = useExperiments();
  if (isLoading) return <LoadingBlock rows={6} />;
  if (error) return <ErrorState error={error} />;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Experiments</CardTitle>
        <CardDescription>Baseline vs candidate on matched cases. Unsuccessful experiments are kept. Start one from Improvements.</CardDescription>
      </CardHeader>
      <CardContent>
        {data?.items.length ? (
          <Table data-testid="experiments">
            <THead><tr><TH>Experiment</TH><TH>Agent</TH><TH>Candidate</TH><TH>Decision</TH><TH>Success (base → cand)</TH><TH>Cost/success</TH><TH>Regressions</TH><TH>n</TH><TH>Created</TH></tr></THead>
            <TBody>{data.items.map((e) => {
              const r = (e.results ?? {}) as R;
              return (
                <TR key={e.id}>
                  <TD><Link className="font-mono text-primary hover:underline" href={`/autopilot/experiments/${e.id}`}>{shortId(e.id)}</Link></TD>
                  <TD>{e.agent_name}</TD>
                  <TD className="text-xs">v{e.baseline_version} → v{e.candidate_version}{e.candidate_title ? ` · ${e.candidate_title}` : ""}</TD>
                  <TD>{e.status === "running" ? <DecisionBadge value="running" /> : <DecisionBadge value={e.decision} />}</TD>
                  <TD>{pct(r.baseline?.metrics?.task_success_rate?.value)} → {pct(r.candidate?.metrics?.task_success_rate?.value)}</TD>
                  <TD className="text-xs">{formatCost(r.cost?.baseline_per_success)} → {formatCost(r.cost?.candidate_per_success)}</TD>
                  <TD>{r.comparison?.regressions?.length ?? "—"}</TD><TD>{r.comparison?.n_matched ?? "—"}</TD>
                  <TD className="text-muted-foreground">{relativeTime(e.created_at)}</TD>
                </TR>);
            })}</TBody>
          </Table>
        ) : <EmptyState title="No experiments yet" description="Generate candidates on the Improvements tab, then run an experiment." />}
      </CardContent>
    </Card>
  );
}
