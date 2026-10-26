"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useFlightRuns } from "@/lib/flight";
import { formatDuration, formatTime, shortId } from "@/lib/format";

export function FlightIndex() {
  const router = useRouter();
  const runs = useFlightRuns("workflow");
  const replays = useFlightRuns("replay");
  const [picked, setPicked] = useState<string[]>([]);
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));
  if (runs.isLoading) return <LoadingBlock rows={6} />;
  if (runs.error) return <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />;
  const rows = [...(replays.data?.items ?? []), ...(runs.data?.items ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at));
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Flight Recorder</h1>
        <p className="text-sm text-muted-foreground">
          Replay any run from its immutable recorded events: step through, scrub the timeline, inspect sanitized model calls and tool I/O.
          Viewing a recording executes nothing. <Badge tone="info">recorded replay</Badge>{" "}
          <Badge tone="warning">deterministic re-execution</Badge> creates a separate, side-effect-free run.
        </p>
      </div>
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <div>
            <CardTitle>Runs</CardTitle>
            <CardDescription>Select two runs to compare them side by side.</CardDescription>
          </div>
          <Button size="sm" disabled={picked.length !== 2} data-testid="compare-runs" onClick={() => router.push(`/flight/compare?a=${picked[0]}&b=${picked[1]}`)}>
            Compare selected ({picked.length}/2)
          </Button>
        </CardHeader>
        <CardContent>
          {rows.length ? (
            <Table data-testid="flight-runs">
              <THead><tr><TH /><TH>Run</TH><TH>Workflow</TH><TH>Kind</TH><TH>Status</TH><TH>Duration</TH><TH>Started</TH></tr></THead>
              <TBody>
                {rows.map((r) => (
                  <TR key={r.id}>
                    <TD><input type="checkbox" aria-label={`Select run ${shortId(r.id)} for comparison`} checked={picked.includes(r.id)} onChange={() => toggle(r.id)} /></TD>
                    <TD><Link className="font-mono text-primary hover:underline" href={`/flight/${r.id}`}>{shortId(r.id)}</Link></TD>
                    <TD>{r.workflow_name}</TD>
                    <TD>{replays.data?.items.some((x) => x.id === r.id) ? <Badge tone="warning">re-execution</Badge> : <Badge>recorded</Badge>}</TD>
                    <TD><StatusBadge status={r.status} /></TD>
                    <TD>{formatDuration(r.duration_ms)}</TD>
                    <TD className="text-muted-foreground">{formatTime(r.created_at)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          ) : <EmptyState title="No runs yet" description="Start a workflow, then replay it here." />}
        </CardContent>
      </Card>
    </div>
  );
}
