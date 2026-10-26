import { formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { RunEvent } from "@/types";

const TONE: Record<string, string> = {
  "run.completed": "text-success",
  "step.completed": "text-success",
  "tool.completed": "text-success",
  "run.failed": "text-danger",
  "step.failed": "text-danger",
  "tool.failed": "text-danger",
  "tool.denied": "text-danger",
  "model.call.failed": "text-danger",
  "step.stale_result_rejected": "text-danger",
  "step.retrying": "text-warning",
  "approval.requested": "text-warning",
  "run.waiting_approval": "text-warning",
  "budget.exceeded": "text-danger",
  "run.cancelled": "text-muted-foreground",
};

export function describe(e: RunEvent): string {
  const p = e.payload as Record<string, unknown>;
  const node = p.node_key ? `${p.node_key}` : "";
  switch (e.event_type) {
    case "step.started":
      return `${node} attempt ${p.attempt} on ${p.worker_id}${p.resumed ? " (resumed)" : ""}`;
    case "step.retrying":
      return `${node} retry ${p.failure_count}/${p.max_attempts} in ${p.delay_seconds}s — ${(p.error as { type?: string })?.type}`;
    case "step.failed":
      return `${node} failed — ${(p.error as { message?: string })?.message ?? ""}`;
    case "model.call.completed":
      return `${node} ${p.provider}/${p.model} ${p.input_tokens ?? "?"}→${p.output_tokens ?? "?"} tok, ${p.latency_ms} ms${
        p.simulated ? " (simulated)" : ""
      }`;
    case "model.call.failed":
      return `${node} ${p.provider} call failed — ${(p.error as { type?: string })?.type}`;
    case "tool.started":
    case "tool.completed":
    case "tool.failed":
    case "tool.denied":
      return `${node} ${p.tool}${p.error ? ` — ${(p.error as { message?: string }).message}` : ""}`;
    case "approval.requested":
      return `${node} requests: ${p.action}`;
    case "approval.resolved":
      return `${node} approval ${p.status}${p.reason ? ` — ${p.reason}` : ""}`;
    default:
      return node;
  }
}

export function EventFeed({ events, max = 200 }: { events: RunEvent[]; max?: number }) {
  const shown = events.slice(-max).reverse();
  if (!shown.length) return <p className="text-sm text-muted-foreground">No events yet.</p>;
  return (
    <ol className="flex flex-col divide-y text-xs" aria-label="Run events" data-testid="event-feed">
      {shown.map((e) => (
        <li key={e.sequence_number} className="flex gap-3 py-1.5" data-event-type={e.event_type}>
          <span className="w-8 shrink-0 text-right font-mono text-muted-foreground">#{e.sequence_number}</span>
          <span className="w-36 shrink-0 text-muted-foreground">{formatTime(e.timestamp)}</span>
          <span className={cn("w-44 shrink-0 font-mono", TONE[e.event_type])}>{e.event_type}</span>
          <span className="min-w-0 flex-1 truncate" title={describe(e)}>
            {describe(e)}
          </span>
        </li>
      ))}
    </ol>
  );
}
