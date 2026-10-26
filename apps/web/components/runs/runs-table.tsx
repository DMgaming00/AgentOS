import Link from "next/link";

import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { formatCost, formatDuration, relativeTime, shortId } from "@/lib/format";
import type { Run } from "@/types";

export function RunsTable({ runs }: { runs: Run[] }) {
  return (
    <Table data-testid="runs-table">
      <THead>
        <tr>
          <TH>Run</TH>
          <TH>Workflow</TH>
          <TH>Status</TH>
          <TH>Steps</TH>
          <TH>Duration</TH>
          <TH>Cost</TH>
          <TH>Created</TH>
        </tr>
      </THead>
      <TBody>
        {runs.map((r) => {
          const total = Object.values(r.step_counts).reduce((a, b) => a + b, 0);
          return (
            <TR key={r.id}>
              <TD>
                <Link href={`/runs/${r.id}`} className="font-mono text-primary hover:underline">
                  {shortId(r.id)}
                </Link>
              </TD>
              <TD>
                {r.workflow_name} <span className="text-xs text-muted-foreground">v{r.workflow_version}</span>
              </TD>
              <TD>
                <StatusBadge status={r.status} />
              </TD>
              <TD className="tabular-nums">
                {r.step_counts.succeeded ?? 0}/{total}
                {r.step_counts.failed ? <span className="text-danger"> · {r.step_counts.failed} failed</span> : null}
              </TD>
              <TD className="tabular-nums">{formatDuration(r.duration_ms)}</TD>
              <TD className="tabular-nums">{formatCost(r.cost_spent_usd)}</TD>
              <TD className="text-muted-foreground">{relativeTime(r.created_at)}</TD>
            </TR>
          );
        })}
      </TBody>
    </Table>
  );
}
