"use client";

import { CheckCircle2, Cpu, DollarSign, Loader2, PlayCircle, XCircle } from "lucide-react";
import Link from "next/link";

import { RunsTable } from "@/components/runs/runs-table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatCard } from "@/components/ui/stat";
import { formatCost, formatDuration, relativeTime } from "@/lib/format";
import { useMetrics, useRuns } from "@/lib/queries";

type Overview = {
  total_runs: number; running: number; waiting_approval: number; succeeded: number; failed: number; cancelled: number;
  avg_duration_ms: number | null; p95_duration_ms: number | null; estimated_cost_usd: number; includes_simulated_cost: boolean;
  active_workers: number; runs_by_status: Record<string, number>;
  recent_activity: { run_id: string; workflow: string; event_type: string; timestamp: string; sequence_number: number; node_key?: string }[];
};

export default function OverviewPage() {
  const overview = useMetrics<Overview>("overview", 30, 5000);
  const runs = useRuns({ limit: 8 });
  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Overview</h1>
          <p className="text-sm text-muted-foreground">Last 30 days, computed from persisted run records.</p>
        </div>
        <Button asChild>
          <Link href="/workflows">
            <PlayCircle aria-hidden /> Start a workflow
          </Link>
        </Button>
      </div>
      {overview.error ? <ErrorState error={overview.error} onRetry={() => void overview.refetch()} /> : null}
      {overview.isLoading ? <LoadingBlock rows={2} /> : overview.data ? (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" data-testid="overview-stats">
          <StatCard label="Total runs" value={overview.data.total_runs} />
          <StatCard label="Running" value={overview.data.running} tone="info" icon={<Loader2 className="size-4" aria-hidden />}
                    hint={`${overview.data.waiting_approval} waiting approval`} />
          <StatCard label="Succeeded" value={overview.data.succeeded} tone="success" icon={<CheckCircle2 className="size-4" aria-hidden />} />
          <StatCard label="Failed" value={overview.data.failed} tone="danger" icon={<XCircle className="size-4" aria-hidden />}
                    hint={`${overview.data.cancelled} cancelled`} />
          <StatCard label="Estimated cost" value={formatCost(overview.data.estimated_cost_usd)} icon={<DollarSign className="size-4" aria-hidden />}
                    hint={overview.data.includes_simulated_cost ? "includes simulated (mock) cost" : "from reported usage"} />
          <StatCard label="Active workers" value={overview.data.active_workers} icon={<Cpu className="size-4" aria-hidden />}
                    hint={`avg run ${formatDuration(overview.data.avg_duration_ms)}`} />
        </div>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle>Recent runs</CardTitle>
            <Link href="/runs" className="text-xs text-primary hover:underline">View all</Link>
          </CardHeader>
          <CardContent>
            {runs.isLoading ? <LoadingBlock /> : runs.error ? <ErrorState error={runs.error} /> :
              runs.data?.items.length ? <RunsTable runs={runs.data.items} /> :
              <EmptyState title="No runs yet" description="Start the research-report demo from the Workflows page."
                          action={<Button asChild size="sm"><Link href="/workflows">Open workflows</Link></Button>} />}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle>Live activity</CardTitle>
            <Badge tone="success">auto-refresh 5s</Badge>
          </CardHeader>
          <CardContent>
            {overview.data?.recent_activity.length ? (
              <ol className="flex flex-col gap-2 text-sm" aria-label="Recent activity">
                {overview.data.recent_activity.map((a) => (
                  <li key={`${a.run_id}-${a.sequence_number}`} className="flex items-start justify-between gap-2">
                    <Link href={`/runs/${a.run_id}`} className="min-w-0 hover:underline">
                      <span className="font-mono text-xs">{a.event_type}</span>
                      <span className="block truncate text-xs text-muted-foreground">
                        {a.workflow}{a.node_key ? ` · ${a.node_key}` : ""}
                      </span>
                    </Link>
                    <span className="shrink-0 text-xs text-muted-foreground">{relativeTime(a.timestamp)}</span>
                  </li>
                ))}
              </ol>
            ) : <p className="text-sm text-muted-foreground">No activity yet.</p>}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
