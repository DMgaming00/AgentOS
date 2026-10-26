import { cn } from "@/lib/utils";
import { Card } from "./card";

export function StatCard({
  label,
  value,
  hint,
  tone,
  icon,
}: {
  label: string;
  value: React.ReactNode;
  hint?: React.ReactNode;
  tone?: "success" | "danger" | "warning" | "info";
  icon?: React.ReactNode;
}) {
  const color = tone ? { success: "text-success", danger: "text-danger", warning: "text-warning", info: "text-info" }[tone] : "";
  return (
    <Card className="p-4">
      <div className="flex items-center justify-between text-xs text-muted-foreground">
        <span>{label}</span>
        {icon}
      </div>
      <div className={cn("mt-2 text-2xl font-semibold tabular-nums", color)}>{value}</div>
      {hint ? <div className="mt-1 text-xs text-muted-foreground">{hint}</div> : null}
    </Card>
  );
}
