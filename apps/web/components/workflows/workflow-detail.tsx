"use client";

import { useMutation } from "@tanstack/react-query";
import { Play } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { RunsTable } from "@/components/runs/runs-table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
import { ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Label, Textarea } from "@/components/ui/inputs";
import { JsonView } from "@/components/ui/json";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { canWrite, useAuth } from "@/lib/auth";
import { formatTime } from "@/lib/format";
import { useRuns, useWorkflow, useWorkflowVersions } from "@/lib/queries";
import { DagGraph, type DagNodeSpec } from "./dag-graph";

export function WorkflowDetailView() {
  const { id } = useParams<{ id: string }>();
  const wf = useWorkflow(id);
  const versions = useWorkflowVersions(id);
  const runs = useRuns({ workflow_id: id, limit: 10 });
  const { role } = useAuth();
  if (wf.isLoading) return <LoadingBlock rows={6} />;
  if (wf.error || !wf.data) return <ErrorState error={wf.error} onRetry={() => void wf.refetch()} />;
  const w = wf.data;
  const nodes = ((w.definition as { nodes?: DagNodeSpec[] }).nodes ?? []) as DagNodeSpec[];
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <p className="text-xs text-muted-foreground"><Link href="/workflows" className="hover:underline">Workflows</Link></p>
          <h1 className="text-xl font-semibold">{w.name} <span className="text-base text-muted-foreground">v{w.version}</span></h1>
          <p className="text-sm text-muted-foreground">{w.description}</p>
        </div>
        <div className="flex gap-2">
          {w.validation.valid ? <Badge tone="success">valid</Badge> : <Badge tone="danger">invalid</Badge>}
          {canWrite(role) ? <StartRun workflowId={id} disabled={!w.validation.valid} /> : null}
        </div>
      </div>
      {!w.validation.valid ? (
        <ErrorState error={new Error(w.validation.issues.map((i) => `${i.node ?? "-"}: ${i.message}`).join("; "))} />
      ) : null}
      <Card>
        <CardHeader><CardTitle>Graph</CardTitle></CardHeader>
        <CardContent><DagGraph nodes={nodes} height={380} /></CardContent>
      </Card>
      <div className="grid gap-4 xl:grid-cols-3">
        <Card className="xl:col-span-2">
          <CardHeader><CardTitle>Recent runs of this version</CardTitle></CardHeader>
          <CardContent>{runs.data?.items.length ? <RunsTable runs={runs.data.items} /> : <p className="text-sm text-muted-foreground">No runs yet.</p>}</CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Versions</CardTitle></CardHeader>
          <CardContent>
            <ul className="flex flex-col gap-1 text-sm">
              {versions.data?.map((v) => (
                <li key={v.id}>
                  <Link href={`/workflows/${v.id}`} className={v.id === id ? "font-semibold" : "text-primary hover:underline"}>
                    v{v.version}
                  </Link>{" "}
                  <span className="text-xs text-muted-foreground">{formatTime(v.created_at)}</span>
                </li>
              ))}
            </ul>
            <Button asChild variant="outline" size="sm" className="mt-3">
              <Link href={`/workflows/new?from=${id}`}>Edit as new version</Link>
            </Button>
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader><CardTitle>Definition</CardTitle></CardHeader>
        <CardContent><JsonView value={w.definition} /></CardContent>
      </Card>
    </div>
  );
}

function StartRun({ workflowId, disabled }: { workflowId: string; disabled: boolean }) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("{}");
  const key = useRef(idempotencyKey());
  const start = useMutation({
    mutationFn: async () => {
      const parsed = JSON.parse(input) as Record<string, never>;
      return unwrap(await api.POST("/api/v1/workflows/{workflow_id}/runs", {
        params: { path: { workflow_id: workflowId }, header: { "Idempotency-Key": key.current } },
        body: { input: parsed },
      }));
    },
    onSuccess: (run) => router.push(`/runs/${run.id}`),
  });
  return (
    <Dialog open={open} onOpenChange={(o) => { setOpen(o); if (o) key.current = idempotencyKey(); }}>
      <DialogTrigger asChild>
        <Button disabled={disabled} data-testid="start-run"><Play aria-hidden /> Start run</Button>
      </DialogTrigger>
      <DialogContent title="Start a run" description="Optional run input, available to nodes as {{input.*}} and $ref input.*">
        <Label htmlFor="run-input">Input JSON</Label>
        <Textarea id="run-input" value={input} onChange={(e) => setInput(e.target.value)} className="font-mono text-xs" />
        <InlineError error={start.error} />
        <div className="flex justify-end">
          <Button onClick={() => start.mutate()} disabled={start.isPending} data-testid="confirm-start-run">
            {start.isPending ? "Starting…" : "Start"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
