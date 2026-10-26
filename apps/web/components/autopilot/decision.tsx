import { Badge } from "@/components/ui/badge";

const TONE: Record<string, "success" | "danger" | "warning" | "neutral" | "info" | "primary"> = {
  eligible: "success", not_eligible: "danger", inconclusive: "warning", proposed: "info", rejected_policy: "danger",
  evaluating: "primary", promoted: "success", superseded: "neutral", running: "primary", completed: "neutral",
  failed: "danger", pending: "warning", approved: "success", rejected: "danger", expired: "neutral",
  active: "success", rolled_back: "danger", hypothesis: "info", invalid: "danger", queued: "neutral",
};

export function DecisionBadge({ value }: { value: string | null | undefined }) {
  if (!value) return <Badge>—</Badge>;
  return <Badge tone={TONE[value] ?? "neutral"} data-decision={value}>{value.replace(/_/g, " ")}</Badge>;
}
