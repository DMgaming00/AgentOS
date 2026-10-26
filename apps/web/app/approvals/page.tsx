"use client";

import Link from "next/link";

import { ApprovalCard } from "@/components/approvals/approval-card";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { formatTime, shortId } from "@/lib/format";
import { useApprovals } from "@/lib/queries";

export default function ApprovalsPage() {
  const pending = useApprovals({ status: "pending", limit: 100 });
  const history = useApprovals({ status: "approved,rejected,expired", limit: 50 });
  return (
    <div className="flex flex-col gap-5">
      <div>
        <h1 className="text-xl font-semibold">Approvals</h1>
        <p className="text-sm text-muted-foreground">
          Sensitive tool calls pause here. An approval is bound to the exact arguments (hash) and can be used once.
        </p>
      </div>
      <section aria-labelledby="pending-h" className="flex flex-col gap-3">
        <h2 id="pending-h" className="text-sm font-semibold">Pending ({pending.data?.total ?? 0})</h2>
        {pending.isLoading ? <LoadingBlock /> : pending.error ? <ErrorState error={pending.error} /> :
          pending.data?.items.length ? (
            <div className="grid gap-3 xl:grid-cols-2">{pending.data.items.map((a) => <ApprovalCard key={a.id} approval={a} />)}</div>
          ) : <EmptyState title="Nothing waiting for approval" description="Runs that request approval-gated tools will appear here." />}
      </section>
      <Card>
        <CardHeader><CardTitle>Decision history</CardTitle></CardHeader>
        <CardContent>
          {history.isLoading ? <LoadingBlock /> : history.error ? <ErrorState error={history.error} /> : history.data?.items.length ? (
            <Table data-testid="approval-history">
              <THead><tr><TH>Action</TH><TH>Run</TH><TH>Agent</TH><TH>Decision</TH><TH>By</TH><TH>When</TH><TH>Note</TH></tr></THead>
              <TBody>
                {history.data.items.map((a) => (
                  <TR key={a.id}>
                    <TD className="max-w-72 truncate" title={a.requested_action}>{a.requested_action}</TD>
                    <TD><Link href={`/runs/${a.run_id}`} className="font-mono text-primary hover:underline">{shortId(a.run_id)}</Link></TD>
                    <TD>{a.requesting_agent ?? "—"}</TD>
                    <TD><StatusBadge status={a.status} /></TD>
                    <TD>{a.decided_by_email ?? "system"}</TD>
                    <TD className="text-muted-foreground">{formatTime(a.decided_at)}</TD>
                    <TD className="max-w-60 truncate text-muted-foreground" title={a.decision_reason ?? ""}>{a.decision_reason ?? "—"}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          ) : <p className="text-sm text-muted-foreground">No decisions yet.</p>}
        </CardContent>
      </Card>
    </div>
  );
}
