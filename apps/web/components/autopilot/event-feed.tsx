"use client";

import { Radio } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { relativeTime } from "@/lib/format";
import type { AutopilotEvent } from "@/types/autopilot";

function line(e: AutopilotEvent): string {
  const p = e.payload as Record<string, unknown>;
  switch (e.event_type) {
    case "diagnosis.completed":
      return `${p.category} hypothesis (${p.confidence})${p.simulated ? " · simulated" : ""}`;
    case "diagnosis.rejected":
      return `diagnosis rejected: ${(p.errors as string[] | undefined)?.[0] ?? ""}`;
    case "candidate.generated":
      return `${p.title} → v${p.version}`;
    case "candidate.rejected":
      return `${p.title}: ${(p.violations as string[] | undefined)?.[0] ?? "policy violation"}`;
    case "evaluation.started":
      return `${p.agent} v${p.version} on ${p.dataset} (${p.executions} executions${p.arm ? `, ${p.arm}` : ""})`;
    case "evaluation.completed": {
      const r = p.success as { numerator?: number; denominator?: number } | undefined;
      return `${p.arm ?? "evaluation"}: ${r?.numerator}/${r?.denominator} passed${p.incomplete ? `, ${p.incomplete} not run` : ""}`;
    }
    case "experiment.completed":
      return String(p.summary ?? p.decision);
    case "version.promoted":
      return `${p.agent} → v${p.version}`;
    case "version.rolled_back":
      return `${p.agent} restored v${p.restored_version}: ${p.reason}`;
    case "monitoring.regression_detected":
      return "live failure rate rose after deployment";
    default:
      return String(p.agent ?? p.name ?? p.mode ?? "");
  }
}

export function AutopilotEventFeed({ events, state, max = 40 }: { events: AutopilotEvent[]; state: string; max?: number }) {
  return (
    <div className="flex flex-col gap-2">
      <Badge tone={state === "open" ? "success" : "warning"} className="self-start" data-testid="autopilot-stream" data-state={state}>
        <Radio className="size-3" aria-hidden /> {state === "open" ? "Live" : state}
      </Badge>
      {events.length ? (
        <ol className="flex flex-col divide-y text-xs" aria-label="Autopilot activity" data-testid="autopilot-events">
          {events.slice(-max).reverse().map((e) => (
            <li key={e.sequence_number} className="flex gap-3 py-1.5" data-event-type={e.event_type}>
              <span className="w-44 shrink-0 font-mono">{e.event_type}</span>
              <span className="min-w-0 flex-1 truncate" title={line(e)}>{line(e)}</span>
              <span className="shrink-0 text-muted-foreground">{relativeTime(e.created_at)}</span>
            </li>
          ))}
        </ol>
      ) : <p className="text-sm text-muted-foreground">No Autopilot activity yet.</p>}
    </div>
  );
}
