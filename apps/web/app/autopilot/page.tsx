"use client";

import Link from "next/link";

import { AutopilotEventFeed } from "@/components/autopilot/event-feed";
import { DecisionBadge } from "@/components/autopilot/decision";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatCard } from "@/components/ui/stat";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { formatCost } from "@/lib/format";
import { pct, useAutopilotEvents, useAutopilotOverview } from "@/lib/autopilot";

type Overview = {
  agents_monitored: number; failures_detected: number; agent_failures: number; infrastructure_failures: number;
  active_investigations: number; candidates: number; candidates_rejected_by_policy: number; experiments_completed: number;
  experiments_not_eligible: number; evaluations_running: number; approved_deployments: number; rollbacks: number;
  pending_approvals: number; regressions_detected: number; autopilot_spend_usd: number;
  measured_improvements: { experiment_id: string; baseline: number; candidate: number; demonstrated: boolean }[];
  agents: { agent_id: string; name: string; active_version: number | null; canary_percent: number; signals_7d: number;
            latest_candidate: { id: string; title: string; status: string } | null }[];
};

export default function AutopilotOverviewPage() {
  const { data, error, isLoading, refetch } = useAutopilotOverview();
  const { events, state } = useAutopilotEvents();
  if (isLoading) return <LoadingBlock rows={6} />;
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />;
  const o = data as unknown as Overview;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-8" data-testid="autopilot-stats">
        <StatCard label="Agents monitored" value={o.agents_monitored} />
        <StatCard label="Failures detected" value={o.failures_detected}
                  hint={`${o.agent_failures} agent · ${o.infrastructure_failures} infrastructure`} />
        <StatCard label="Active investigations" value={o.active_investigations} hint="diagnoses (hypotheses)" />
        <StatCard label="Candidates" value={o.candidates} hint={`${o.candidates_rejected_by_policy} rejected by policy`} />
        <StatCard label="Experiments" value={o.experiments_completed} hint={`${o.experiments_not_eligible} not eligible`} />
        <StatCard label="Deployments" value={o.approved_deployments} hint={`${o.pending_approvals} awaiting approval`} />
        <StatCard label="Regressions" value={o.regressions_detected} tone={o.regressions_detected ? "danger" : undefined}
                  hint={`${o.rollbacks} rollbacks`} />
        <StatCard label="Autopilot spend" value={formatCost(o.autopilot_spend_usd)} hint="diagnosis + optimizer calls" />
      </div>
      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Agents</CardTitle>
            <CardDescription>Active version, recent failure signals and the latest proposed change.</CardDescription>
          </CardHeader>
          <CardContent>
            <Table>
              <THead><tr><TH>Agent</TH><TH>Active</TH><TH>Signals (7d)</TH><TH>Latest candidate</TH></tr></THead>
              <TBody>
                {o.agents.map((a) => (
                  <TR key={a.agent_id}>
                    <TD><Link className="text-primary hover:underline" href={`/autopilot/improvements?agent=${a.agent_id}`}>{a.name}</Link></TD>
                    <TD>v{a.active_version ?? "—"}{a.canary_percent ? ` (+canary ${a.canary_percent}%)` : ""}</TD>
                    <TD>{a.signals_7d}</TD>
                    <TD>{a.latest_candidate ? <span className="flex items-center gap-2">{a.latest_candidate.title}
                      <DecisionBadge value={a.latest_candidate.status} /></span> : "—"}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Measured impact</CardTitle>
            <CardDescription>Eligible experiments only; success rate on matched validation cases.</CardDescription>
          </CardHeader>
          <CardContent>
            {o.measured_improvements.length ? (
              <ul className="flex flex-col gap-2 text-sm">
                {o.measured_improvements.map((m) => (
                  <li key={m.experiment_id} className="flex items-center justify-between gap-2">
                    <Link href={`/autopilot/experiments/${m.experiment_id}`} className="text-primary hover:underline">
                      {pct(m.baseline)} → {pct(m.candidate)}
                    </Link>
                    <span className="text-xs text-muted-foreground">
                      {m.demonstrated ? "improvement demonstrated (CI excludes 0)" : "non-inferior, no improvement shown"}
                    </span>
                  </li>
                ))}
              </ul>
            ) : <p className="text-sm text-muted-foreground">No eligible experiments yet.</p>}
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader><CardTitle>Live Autopilot activity</CardTitle></CardHeader>
        <CardContent><AutopilotEventFeed events={events} state={state} /></CardContent>
      </Card>
    </div>
  );
}
