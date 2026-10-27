"use client";

import { Cpu } from "lucide-react";
import { useQuery } from "@tanstack/react-query";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatCard } from "@/components/ui/stat";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api, unwrap } from "@/lib/api";
import type { WorkersView } from "@/types/ops";

const pct = (v: number | null) => (v == null ? "n/a" : `${Math.round(v * 100)}%`);

function Bar({ value }: { value: number | null }) {
  return (
    <div className="flex items-center gap-2" aria-label={`utilization ${pct(value)}`}>
      <div className="h-2 w-24 rounded-sm bg-muted" aria-hidden><div className="h-2 rounded-sm bg-primary" style={{ width: `${Math.min(100, (value ?? 0) * 100)}%` }} /></div>
      <span className="text-xs">{pct(value)}</span>
    </div>
  );
}

export function WorkersPage() {
  const q = useQuery({ queryKey: ["workers"], queryFn: async () => unwrap(await api.GET("/api/v1/workers", { params: { query: {} } })) as unknown as WorkersView, refetchInterval: 3000 });
  if (q.isLoading) return <LoadingBlock rows={5} />;
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  const w = q.data!;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold"><Cpu className="size-5" aria-hidden /> Workers</h1>
        <p className="text-sm text-muted-foreground">The worker fleet from heartbeats and recent attempts. Any worker can run any step; leases and heartbeats are what make a crashed worker&apos;s steps recoverable.</p>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="worker-tiles">
        <StatCard label="Healthy workers" value={`${w.summary.healthy}/${w.summary.total}`} hint="heartbeat under 30 s" />
        <StatCard label="Capacity" value={w.summary.capacity} hint="concurrent steps across healthy workers" />
        <StatCard label="In flight" value={w.summary.in_flight} hint="steps running now" />
        <StatCard label="Utilization" value={w.summary.capacity ? pct(w.summary.in_flight / w.summary.capacity) : "n/a"} hint="in flight / capacity" />
      </div>
      <Card>
        <CardHeader><CardTitle>Fleet</CardTitle><CardDescription>{w.note}</CardDescription></CardHeader>
        <CardContent>
          {w.items.length === 0 ? <EmptyState title="No workers have registered" description="Start a worker (docker compose up worker) to run steps." /> : (
            <Table data-testid="worker-table">
              <THead><tr><TH>Worker</TH><TH>Health</TH><TH>Utilization</TH><TH>Heartbeat</TH><TH>Last {w.items[0].recent.window_minutes} min</TH><TH>Queue wait (mean)</TH><TH>Completed (lifetime)</TH></tr></THead>
              <TBody>{w.items.map((x) => (
                <TR key={x.id} data-healthy={String(x.healthy)}>
                  <TD><div className="font-mono text-xs">{x.id}</div><div className="text-[11px] text-muted-foreground">{x.hostname} · pid {x.pid}</div></TD>
                  <TD>{x.healthy ? <Badge tone="success">healthy</Badge> : <Badge tone={x.status === "stopped" ? "neutral" : "danger"}>{x.status === "active" ? "missed heartbeats" : x.status}</Badge>}</TD>
                  <TD><Bar value={x.utilization} /><span className="text-[11px] text-muted-foreground">{x.in_flight}/{x.concurrency} slots</span></TD>
                  <TD className="text-xs">{x.heartbeat_age_s < 1 ? "just now" : `${Math.round(x.heartbeat_age_s)} s ago`}</TD>
                  <TD className="text-xs">{x.recent.attempts} attempts · {x.recent.succeeded} ok · {x.recent.failed} failed</TD>
                  <TD className="text-xs">{x.recent.mean_queue_wait_ms == null ? "n/a" : `${Math.round(x.recent.mean_queue_wait_ms)} ms`}</TD>
                  <TD>{x.steps_completed}</TD>
                </TR>))}</TBody>
            </Table>)}
        </CardContent>
      </Card>
    </div>
  );
}
