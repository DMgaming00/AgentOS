"use client";

import Link from "next/link";
import { useParams } from "next/navigation";

import { AgentForm } from "@/components/agents/agent-form";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { canWrite, useAuth } from "@/lib/auth";
import { durationBetween, formatDuration, relativeTime, shortId } from "@/lib/format";
import { useAgent, useAgentExecutions } from "@/lib/queries";

export function AgentDetail() {
  const { id } = useParams<{ id: string }>();
  const agent = useAgent(id);
  const execs = useAgentExecutions(id);
  const { role } = useAuth();
  if (agent.isLoading) return <LoadingBlock rows={6} />;
  if (agent.error || !agent.data) return <ErrorState error={agent.error} />;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <p className="text-xs text-muted-foreground"><Link href="/agents" className="hover:underline">Agents</Link></p>
        <h1 className="text-xl font-semibold">{agent.data.name}</h1>
      </div>
      <div className="grid gap-4 xl:grid-cols-5">
        <Card className="xl:col-span-3">
          <CardHeader><CardTitle>Configuration</CardTitle></CardHeader>
          <CardContent>
            {canWrite(role) ? <AgentForm key={agent.data.updated_at} agent={agent.data} /> :
              <p className="text-sm text-muted-foreground">Viewers have read-only access.</p>}
          </CardContent>
        </Card>
        <Card className="xl:col-span-2">
          <CardHeader><CardTitle>Execution history ({execs.data?.total ?? 0})</CardTitle></CardHeader>
          <CardContent>
            {execs.isLoading ? <LoadingBlock /> : execs.data?.items.length ? (
              <Table>
                <THead><tr><TH>Run</TH><TH>Step</TH><TH>Status</TH><TH>Attempts</TH><TH>Duration</TH></tr></THead>
                <TBody>
                  {execs.data.items.map((e) => (
                    <TR key={e.step_id}>
                      <TD><Link className="font-mono text-primary hover:underline" href={`/runs/${e.run_id}?step=${e.node_key}`}>{shortId(e.run_id)}</Link>
                        <span className="block text-xs text-muted-foreground">{e.workflow_name} · {relativeTime(e.started_at)}</span></TD>
                      <TD className="font-mono text-xs">{e.node_key}</TD>
                      <TD><StatusBadge status={e.status} /></TD>
                      <TD>{e.attempt_count}</TD>
                      <TD>{formatDuration(durationBetween(e.started_at, e.completed_at))}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            ) : <p className="text-sm text-muted-foreground">This agent has not run yet.</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
