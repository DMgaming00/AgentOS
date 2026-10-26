"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Ban, Radio, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { useMemo } from "react";

import { ApprovalCard } from "@/components/approvals/approval-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Sheet } from "@/components/ui/dialog";
import { ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { JsonView } from "@/components/ui/json";
import { StatCard } from "@/components/ui/stat";
import { StatusBadge } from "@/components/ui/status";
import { DagGraph, type DagNodeSpec, type DagNodeState } from "@/components/workflows/dag-graph";
import { api, unwrap } from "@/lib/api";
import { canWrite, useAuth } from "@/lib/auth";
import { durationBetween, formatCost, formatDuration, formatTime, shortId } from "@/lib/format";
import { useApprovals, useRun, useRunArtifacts, useRunEvents, useSteps } from "@/lib/queries";
import { ArtifactsList } from "./artifacts-list";
import { EventFeed } from "./event-feed";
import { StepPanel } from "./step-panel";
import { Timeline } from "./timeline";

const ACTIVE = new Set(["pending", "planning", "running", "waiting_approval"]);

export function RunDetailView() {
  const { id } = useParams<{ id: string }>();
  const search = useSearchParams();
  const router = useRouter();
  const selectedKey = search.get("step");
  const run = useRun(id);
  const steps = useSteps(id);
  const artifacts = useRunArtifacts(id);
  const approvals = useApprovals({ run_id: id, status: "pending" });
  const { events, state } = useRunEvents(id);
  const { role } = useAuth();
  const qc = useQueryClient();
  const cancel = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/runs/{run_id}/cancel", { params: { path: { run_id: id } } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["run", id] }),
  });

  const { nodes, states } = useMemo(() => {
    const nodes: DagNodeSpec[] = [];
    const states: Record<string, DagNodeState> = {};
    for (const s of steps.data ?? []) {
      const cfg = s.node_config as { name?: string; agent?: string; tool?: string };
      nodes.push({ key: s.node_key, type: s.node_type, name: cfg.name ?? (s.node_type === "plan" ? "AI planner" : null),
                   agent: cfg.agent, tool: cfg.tool, depends_on: s.dependency_keys });
      states[s.node_key] = { status: s.status, attempts: s.attempt_count, failures: s.failure_count, agentName: s.agent_name };
    }
    return { nodes, states };
  }, [steps.data]);

  const tokens = useMemo(() => {
    let input = 0, output = 0, unknown = 0;
    for (const e of events) {
      if (e.event_type !== "model.call.completed") continue;
      const p = e.payload as { input_tokens?: number | null; output_tokens?: number | null };
      if (p.input_tokens == null || p.output_tokens == null) unknown += 1;
      input += p.input_tokens ?? 0;
      output += p.output_tokens ?? 0;
    }
    return { input, output, unknown };
  }, [events]);
  const simulated = events.some((e) => e.event_type === "model.call.completed" && (e.payload as { simulated?: boolean }).simulated);

  if (run.isLoading || steps.isLoading) return <LoadingBlock rows={8} label="Loading run" />;
  if (run.error) return <ErrorState error={run.error} onRetry={() => void run.refetch()} />;
  if (!run.data) return null;
  const r = run.data;
  const selected = steps.data?.find((s) => s.node_key === selectedKey) ?? null;
  const select = (key: string | null) => {
    const params = new URLSearchParams(search.toString());
    if (key) params.set("step", key);
    else params.delete("step");
    router.replace(`/runs/${id}${params.size ? `?${params}` : ""}`, { scroll: false });
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-col gap-1">
          <p className="text-xs text-muted-foreground">
            <Link href="/runs" className="hover:underline">Runs</Link> /{" "}
            <Link href={`/workflows/${r.workflow_definition_id}`} className="hover:underline">{r.workflow_name} v{r.workflow_version}</Link>
          </p>
          <h1 className="flex items-center gap-3 text-xl font-semibold">
            Run <span className="font-mono">{shortId(r.id)}</span>
            <span data-testid="run-status"><StatusBadge status={r.status} /></span>
          </h1>
          <p className="text-xs text-muted-foreground">
            Created {formatTime(r.created_at)}{r.trace_id ? ` · trace ${r.trace_id}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <StreamIndicator state={state} />
          <Button variant="outline" size="sm" onClick={() => { void run.refetch(); void steps.refetch(); }}>
            <RefreshCw aria-hidden /> Refresh
          </Button>
          {ACTIVE.has(r.status) && canWrite(role) ? (
            <Button variant="destructive" size="sm" onClick={() => cancel.mutate()} disabled={cancel.isPending}>
              <Ban aria-hidden /> Cancel run
            </Button>
          ) : null}
        </div>
      </div>
      <InlineError error={cancel.error} />

      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 lg:grid-cols-6">
        <StatCard label="Duration" value={formatDuration(r.duration_ms ?? durationBetween(r.started_at, r.completed_at))} />
        <StatCard label="Steps" value={`${r.step_counts.succeeded ?? 0}/${Object.values(r.step_counts).reduce((a, b) => a + b, 0)}`} hint="succeeded / total" />
        <StatCard label="Model calls" value={`${r.model_calls}/${(r.budget as { max_model_calls?: number }).max_model_calls ?? "∞"}`} hint="used / budget" />
        <StatCard label="Tool calls" value={r.tool_calls} />
        <StatCard label="Tokens in / out" value={`${tokens.input.toLocaleString()} / ${tokens.output.toLocaleString()}`}
                  hint={tokens.unknown ? `${tokens.unknown} call(s) without reported usage` : simulated ? "simulated (mock provider)" : "reported by provider"} />
        <StatCard label="Estimated cost" value={formatCost(r.cost_spent_usd)}
                  hint={`budget ${formatCost((r.budget as { max_cost_usd?: number }).max_cost_usd)}${simulated ? " · simulated prices" : ""}`} />
      </div>

      {approvals.data?.items.length ? (
        <div className="flex flex-col gap-2">
          {approvals.data.items.map((a) => <ApprovalCard key={a.id} approval={a} compact />)}
        </div>
      ) : null}

      {r.error ? (
        <Card className="border-danger/40">
          <CardHeader><CardTitle className="text-danger">Run error</CardTitle></CardHeader>
          <CardContent><JsonView value={r.error} maxHeight="10rem" /></CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle>Execution graph</CardTitle>
          <span className="text-xs text-muted-foreground">Click a node to inspect attempts, model calls and tool calls</span>
        </CardHeader>
        <CardContent>
          {nodes.length ? <DagGraph nodes={nodes} states={states} selectedKey={selectedKey} onSelect={select} height={420} /> :
            <p className="text-sm text-muted-foreground">No steps yet.</p>}
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Step timeline</CardTitle></CardHeader>
          <CardContent><Timeline steps={steps.data ?? []} /></CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Artifacts</CardTitle></CardHeader>
          <CardContent>
            {artifacts.error ? <ErrorState error={artifacts.error} /> : <ArtifactsList artifacts={artifacts.data ?? []} />}
            {r.output ? (
              <details className="mt-3">
                <summary className="cursor-pointer text-xs text-muted-foreground">Run output</summary>
                <JsonView className="mt-2" value={r.output} />
              </details>
            ) : null}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader><CardTitle>Event log ({events.length})</CardTitle></CardHeader>
        <CardContent className="max-h-96 overflow-y-auto"><EventFeed events={events} /></CardContent>
      </Card>

      <Sheet
        open={!!selected}
        onOpenChange={(o) => !o && select(null)}
        title={selected ? `${(selected.node_config as { name?: string }).name ?? selected.node_key}` : ""}
        description={selected ? <span className="flex items-center gap-2"><span className="font-mono">{selected.node_key}</span><StatusBadge status={selected.status} /></span> : null}
      >
        {selected ? <StepPanel runId={id} stepId={selected.id} /> : null}
      </Sheet>
    </div>
  );
}

function StreamIndicator({ state }: { state: string }) {
  const tone = state === "open" ? "success" : state === "closed" ? "neutral" : "warning";
  const label = state === "open" ? "Live" : state === "closed" ? "Stream ended" : state === "reconnecting" ? "Reconnecting…" : "Connecting…";
  return (
    <Badge tone={tone} aria-live="polite" data-testid="stream-state" data-state={state}>
      <Radio className="size-3" aria-hidden /> {label}
    </Badge>
  );
}
