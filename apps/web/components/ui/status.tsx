import { CheckCircle2, CircleDashed, CircleSlash, Clock, Hourglass, Loader2, PauseCircle, RotateCw, XCircle } from "lucide-react";

import { Badge } from "./badge";

type Tone = "neutral" | "info" | "success" | "warning" | "danger" | "primary";

export const STATUS_META: Record<string, { tone: Tone; label: string; icon: React.ComponentType<{ className?: string }> }> = {
  pending: { tone: "neutral", label: "Pending", icon: CircleDashed },
  planning: { tone: "primary", label: "Planning", icon: Loader2 },
  ready: { tone: "neutral", label: "Ready", icon: Clock },
  running: { tone: "info", label: "Running", icon: Loader2 },
  waiting_approval: { tone: "warning", label: "Waiting approval", icon: PauseCircle },
  retry_wait: { tone: "warning", label: "Retrying", icon: RotateCw },
  waiting_child: { tone: "info", label: "Waiting on children", icon: Hourglass },
  paused: { tone: "warning", label: "Paused", icon: PauseCircle },
  succeeded: { tone: "success", label: "Succeeded", icon: CheckCircle2 },
  failed: { tone: "danger", label: "Failed", icon: XCircle },
  skipped: { tone: "neutral", label: "Skipped", icon: CircleSlash },
  cancelled: { tone: "neutral", label: "Cancelled", icon: CircleSlash },
  approved: { tone: "success", label: "Approved", icon: CheckCircle2 },
  rejected: { tone: "danger", label: "Rejected", icon: XCircle },
  expired: { tone: "neutral", label: "Expired", icon: Hourglass },
  suspended: { tone: "warning", label: "Suspended", icon: PauseCircle },
  lease_expired: { tone: "danger", label: "Lease expired", icon: XCircle },
  stale: { tone: "neutral", label: "Stale (discarded)", icon: CircleSlash },
  denied: { tone: "danger", label: "Denied", icon: XCircle },
  pending_approval: { tone: "warning", label: "Pending approval", icon: PauseCircle },
};

export function StatusBadge({ status }: { status: string }) {
  const meta = STATUS_META[status] ?? { tone: "neutral" as Tone, label: status, icon: CircleDashed };
  const Icon = meta.icon;
  const spinning = status === "running" || status === "planning";
  return (
    <Badge tone={meta.tone} data-status={status}>
      <Icon className={spinning ? "size-3 animate-spin" : "size-3"} aria-hidden />
      {meta.label}
    </Badge>
  );
}
