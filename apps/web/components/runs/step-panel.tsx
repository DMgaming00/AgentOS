"use client";

import { ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { JsonView, KV } from "@/components/ui/json";
import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ApprovalCard } from "@/components/approvals/approval-card";
import { durationBetween, formatCost, formatDuration, formatTime, formatTokens } from "@/lib/format";
import { useStepDetail } from "@/lib/queries";
import { EventFeed } from "./event-feed";

export function StepPanel({ runId, stepId }: { runId: string; stepId: string }) {
  const { data: step, error, isLoading, refetch } = useStepDetail(runId, stepId);
  if (isLoading) return <LoadingBlock rows={6} />;
  if (error || !step) return <ErrorState error={error} onRetry={() => void refetch()} />;
  const responses = step.events.filter((e) => e.event_type === "model.call.completed" || e.event_type === "model.call.failed");
  const totalCost = step.model_usage.reduce((a, u) => a + Number(u.estimated_cost_usd ?? 0), 0);

  return (
    <Tabs defaultValue="overview">
      <TabsList>
        <TabsTrigger value="overview">Overview</TabsTrigger>
        <TabsTrigger value="attempts">Attempts ({step.attempts.length})</TabsTrigger>
        <TabsTrigger value="model">Model calls ({step.model_usage.length})</TabsTrigger>
        <TabsTrigger value="tools">Tool calls ({step.tool_executions.length})</TabsTrigger>
        <TabsTrigger value="approvals">Approvals ({step.approvals.length})</TabsTrigger>
        <TabsTrigger value="events">Events</TabsTrigger>
      </TabsList>

      <TabsContent value="overview" className="flex flex-col gap-4">
        <KV
          items={[
            ["Status", <StatusBadge key="s" status={step.status} />],
            ["Type", step.node_type],
            ["Agent", step.agent_name ?? "—"],
            ["Depends on", step.dependency_keys.length ? step.dependency_keys.join(", ") : "—"],
            ["Attempts", `${step.attempt_count} (failures ${step.failure_count} / max ${step.max_attempts})`],
            ["Started", formatTime(step.started_at)],
            ["Completed", formatTime(step.completed_at)],
            ["Duration", formatDuration(durationBetween(step.started_at, step.completed_at))],
            ["Lease", step.lease_owner ? `${step.lease_owner} until ${formatTime(step.lease_expires_at)}` : "—"],
            ["Next retry", formatTime(step.next_run_at)],
            ["Estimated cost", formatCost(totalCost)],
          ]}
        />
        {step.error ? (
          <div>
            <p className="mb-1 text-xs font-medium text-danger">Error</p>
            <JsonView value={step.error} />
          </div>
        ) : null}
        <div>
          <p className="mb-1 text-xs font-medium text-muted-foreground">Input</p>
          <JsonView value={step.input} maxHeight="12rem" />
        </div>
        <div>
          <p className="mb-1 text-xs font-medium text-muted-foreground">Output</p>
          <JsonView value={step.output} />
        </div>
      </TabsContent>

      <TabsContent value="attempts">
        <Table>
          <THead>
            <tr>
              <TH>#</TH>
              <TH>Worker</TH>
              <TH>Status</TH>
              <TH>Queue wait</TH>
              <TH>Duration</TH>
              <TH>Error</TH>
            </tr>
          </THead>
          <TBody>
            {step.attempts.map((a) => (
              <TR key={a.id} data-testid="attempt-row">
                <TD>{a.attempt_number}</TD>
                <TD className="font-mono text-xs">{a.worker_id}</TD>
                <TD>
                  <StatusBadge status={a.status} />
                </TD>
                <TD>{formatDuration(a.queue_wait_ms)}</TD>
                <TD>{formatDuration(durationBetween(a.started_at, a.finished_at))}</TD>
                <TD className="max-w-60 truncate text-xs" title={a.error_message ?? ""}>
                  {a.error_type ?? "—"}
                </TD>
              </TR>
            ))}
          </TBody>
        </Table>
      </TabsContent>

      <TabsContent value="model" className="flex flex-col gap-3">
        <Table>
          <THead>
            <tr>
              <TH>Model</TH>
              <TH>Tokens in/out</TH>
              <TH>Cached</TH>
              <TH>Latency</TH>
              <TH>Cost</TH>
              <TH>Finish</TH>
            </tr>
          </THead>
          <TBody>
            {step.model_usage.map((u) => (
              <TR key={u.id}>
                <TD className="text-xs">
                  {u.provider}/{u.model}
                </TD>
                <TD>
                  {formatTokens(u.input_tokens)} / {formatTokens(u.output_tokens)}
                </TD>
                <TD>{formatTokens(u.cached_input_tokens)}</TD>
                <TD>{formatDuration(u.latency_ms)}</TD>
                <TD title={`basis: ${u.cost_basis}, pricing ${u.pricing_version}`}>
                  {formatCost(u.estimated_cost_usd)} <span className="text-xs text-muted-foreground">({u.cost_basis})</span>
                </TD>
                <TD>{u.status === "failed" ? <span className="text-danger">{u.error_type}</span> : u.finish_reason}</TD>
              </TR>
            ))}
          </TBody>
        </Table>
        <p className="text-xs font-medium text-muted-foreground">Responses (redacted previews)</p>
        {responses.map((e) => {
          const p = e.payload as { text_preview?: string; tool_calls?: { name: string }[]; error?: unknown };
          return (
            <div key={e.sequence_number} className="rounded-md border p-2 text-xs">
              <p className="mb-1 text-muted-foreground">
                #{e.sequence_number} · {formatTime(e.timestamp)}
                {p.tool_calls?.length ? ` · tool calls: ${p.tool_calls.map((c) => c.name).join(", ")}` : ""}
              </p>
              {p.text_preview ? <JsonView value={p.text_preview} maxHeight="10rem" /> : p.error ? <JsonView value={p.error} /> : null}
            </div>
          );
        })}
      </TabsContent>

      <TabsContent value="tools" className="flex flex-col gap-3">
        {step.tool_executions.length === 0 ? <p className="text-sm text-muted-foreground">No tool calls.</p> : null}
        {step.tool_executions.map((t) => (
          <details key={t.id} className="rounded-md border p-3" data-testid="tool-execution">
            <summary className="flex cursor-pointer items-center justify-between gap-2 text-sm">
              <span className="font-mono">{t.tool_name}</span>
              <span className="flex items-center gap-2">
                {t.requires_approval ? <span className="text-xs text-warning">approval required</span> : null}
                <StatusBadge status={t.status} />
              </span>
            </summary>
            <div className="mt-2 flex flex-col gap-2">
              <p className="text-xs text-muted-foreground">Arguments</p>
              <JsonView value={t.arguments} maxHeight="12rem" />
              <p className="text-xs text-muted-foreground">{t.error ? "Error" : "Result"}</p>
              <JsonView value={t.error ?? t.result} maxHeight="16rem" />
            </div>
          </details>
        ))}
      </TabsContent>

      <TabsContent value="approvals" className="flex flex-col gap-3">
        {step.approvals.length === 0 ? <p className="text-sm text-muted-foreground">No approvals for this step.</p> : null}
        {step.approvals.map((a) => (
          <ApprovalCard key={a.id} approval={a} />
        ))}
      </TabsContent>

      <TabsContent value="events">
        <EventFeed events={step.events} />
      </TabsContent>
    </Tabs>
  );
}
