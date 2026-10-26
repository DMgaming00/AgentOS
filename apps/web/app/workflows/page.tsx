"use client";

import { Plus } from "lucide-react";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatusBadge } from "@/components/ui/status";
import { relativeTime } from "@/lib/format";
import { useWorkflows } from "@/lib/queries";

export default function WorkflowsPage() {
  const wfs = useWorkflows();
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Workflows</h1>
          <p className="text-sm text-muted-foreground">Versioned DAG definitions. Saving a name again creates a new immutable version.</p>
        </div>
        <Button asChild><Link href="/workflows/new"><Plus aria-hidden /> New workflow</Link></Button>
      </div>
      {wfs.isLoading ? <LoadingBlock rows={4} /> : wfs.error ? <ErrorState error={wfs.error} onRetry={() => void wfs.refetch()} /> :
        wfs.data?.items.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {wfs.data.items.map((w) => (
              <Link key={w.id} href={`/workflows/${w.id}`} className="group" data-testid={`workflow-${w.name}`}>
                <Card className="h-full p-4 transition-colors group-hover:border-primary/50">
                  <div className="flex items-start justify-between gap-2">
                    <p className="font-medium">{w.name}</p>
                    <span className="text-xs text-muted-foreground">v{w.version} · {w.version_count} version{w.version_count > 1 ? "s" : ""}</span>
                  </div>
                  <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">{w.description || "No description"}</p>
                  <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
                    <span>{w.node_count} nodes</span>
                    {w.last_run_status ? <span className="flex items-center gap-2"><StatusBadge status={w.last_run_status} /> {relativeTime(w.last_run_at)}</span> : <span>never run</span>}
                  </div>
                </Card>
              </Link>
            ))}
          </div>
        ) : <EmptyState title="No workflows" description="Run `make seed` to load the demo workflows, or create one." />}
    </div>
  );
}
