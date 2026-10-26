"use client";

import { formatDuration } from "@/lib/format";
import { useNow } from "@/lib/queries";
import { cn } from "@/lib/utils";
import type { Step } from "@/types";

const COLOR: Record<string, string> = {
  succeeded: "bg-success", failed: "bg-danger", lease_expired: "bg-danger", running: "bg-info",
  suspended: "bg-warning", stale: "bg-muted-foreground", cancelled: "bg-muted-foreground",
};

/** Gantt-style view of every attempt, so parallelism, retries and recovery are visible. */
export function Timeline({ steps }: { steps: Step[] }) {
  const now = useNow(1000);
  const attempts = steps.flatMap((s) => s.attempts.map((a) => ({ step: s.node_key, ...a })));
  if (!attempts.length) return <p className="text-sm text-muted-foreground">No attempts yet.</p>;
  const start = Math.min(...attempts.map((a) => new Date(a.started_at).getTime()));
  const end = Math.max(...attempts.map((a) => (a.finished_at ? new Date(a.finished_at).getTime() : now)));
  const span = Math.max(1, end - start);
  const first = (s: Step) => Math.min(...s.attempts.map((a) => new Date(a.started_at).getTime()));
  const rows = steps.filter((s) => s.attempts.length).sort((a, b) => first(a) - first(b));
  return (
    <div className="flex flex-col gap-1.5" data-testid="timeline">
      {rows.map((s) => (
        <div key={s.id} className="flex items-center gap-2 text-xs">
          <span className="w-36 shrink-0 truncate font-mono" title={s.node_key}>{s.node_key}</span>
          <div className="relative h-5 flex-1 rounded bg-muted">
            {s.attempts.map((a) => {
              const a0 = new Date(a.started_at).getTime();
              const a1 = a.finished_at ? new Date(a.finished_at).getTime() : now;
              return (
                <div
                  key={a.id}
                  className={cn("absolute top-0.5 h-4 rounded-sm", COLOR[a.status] ?? "bg-primary", a.status === "running" && "animate-pulse")}
                  style={{ left: `${((a0 - start) / span) * 100}%`, width: `max(3px, ${((a1 - a0) / span) * 100}%)` }}
                  title={`attempt ${a.attempt_number} · ${a.status} · ${a.worker_id} · ${formatDuration(a1 - a0)}`}
                />
              );
            })}
          </div>
        </div>
      ))}
      <p className="text-right text-[11px] text-muted-foreground">total span {formatDuration(span)}</p>
    </div>
  );
}
