"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Clock, X } from "lucide-react";
import Link from "next/link";
import { useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { InlineError } from "@/components/ui/feedback";
import { Textarea } from "@/components/ui/inputs";
import { JsonView, KV } from "@/components/ui/json";
import { StatusBadge } from "@/components/ui/status";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { canWrite, useAuth } from "@/lib/auth";
import { formatTime, relativeTime, shortId } from "@/lib/format";
import { useNow } from "@/lib/queries";
import type { Approval } from "@/types";

export function ApprovalCard({ approval, compact = false }: { approval: Approval; compact?: boolean }) {
  const qc = useQueryClient();
  const { role } = useAuth();
  const [reason, setReason] = useState("");
  // One key per decision attempt so a double click or retry cannot apply twice.
  const key = useRef(idempotencyKey());
  const decide = useMutation({
    mutationFn: async (decision: "approve" | "reject") => {
      const path = decision === "approve" ? "/api/v1/approvals/{approval_id}/approve" : "/api/v1/approvals/{approval_id}/reject";
      return unwrap(
        await api.POST(path, {
          params: { path: { approval_id: approval.id }, header: { "Idempotency-Key": `${key.current}:${decision}` } },
          body: { reason: reason || null },
        }),
      );
    },
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: ["approvals"] });
      void qc.invalidateQueries({ queryKey: ["run", approval.run_id] });
      void qc.invalidateQueries({ queryKey: ["steps", approval.run_id] });
    },
  });
  const pending = approval.status === "pending";
  const now = useNow(10_000);
  const expired = new Date(approval.expires_at).getTime() < now;
  const content = approval.sanitized_arguments?.content;

  return (
    <Card className="p-4" data-testid="approval-card" data-approval-id={approval.id}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="flex min-w-0 flex-col gap-1">
          <p className="font-medium">{approval.requested_action}</p>
          <p className="text-xs text-muted-foreground">
            <Link href={`/runs/${approval.run_id}`} className="hover:underline">
              {approval.workflow_name} · run {shortId(approval.run_id)}
            </Link>{" "}
            · step <span className="font-mono">{approval.node_key}</span>
          </p>
        </div>
        <StatusBadge status={approval.status} />
      </div>
      <div className="mt-3">
        <KV
          items={[
            ["Requesting agent", approval.requesting_agent ?? "workflow (approval gate)"],
            ["Tool", approval.tool_name ?? "—"],
            ["Affected resource", approval.affected_resource ?? "—"],
            ["Reason", approval.reason ?? "—"],
            ["Requested", `${formatTime(approval.requested_at)} (${relativeTime(approval.requested_at)})`],
            [
              "Expires",
              <span key="exp" className={expired && pending ? "text-danger" : undefined}>
                <Clock className="mr-1 inline size-3" aria-hidden />
                {formatTime(approval.expires_at)}
              </span>,
            ],
            ...(approval.decided_at
              ? ([
                  ["Decided", `${formatTime(approval.decided_at)} by ${approval.decided_by_email ?? "system"}`],
                  ["Decision note", approval.decision_reason ?? "—"],
                ] as [string, React.ReactNode][])
              : []),
            ["Arguments hash", <span key="h" className="font-mono text-xs">{approval.arguments_hash.slice(0, 16)}…</span>],
          ]}
        />
      </div>
      {!compact ? (
        <details className="mt-3" open={typeof content === "string"}>
          <summary className="cursor-pointer text-xs text-muted-foreground">Proposed action (sanitized arguments)</summary>
          {typeof content === "string" ? (
            <pre className="mt-2 max-h-72 overflow-auto rounded-md border bg-muted/60 p-3 text-xs whitespace-pre-wrap" data-testid="approval-content">
              {content}
            </pre>
          ) : (
            <JsonView className="mt-2" value={approval.sanitized_arguments} />
          )}
        </details>
      ) : null}
      {pending ? (
        canWrite(role) ? (
          <div className="mt-4 flex flex-col gap-2">
            <label htmlFor={`reason-${approval.id}`} className="sr-only">
              Decision note
            </label>
            <Textarea
              id={`reason-${approval.id}`}
              placeholder="Optional note (shown to the agent on rejection)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              className="min-h-14"
            />
            <div className="flex gap-2">
              <Button variant="success" onClick={() => decide.mutate("approve")} disabled={decide.isPending}>
                <Check aria-hidden /> Approve
              </Button>
              <Button variant="destructive" onClick={() => decide.mutate("reject")} disabled={decide.isPending}>
                <X aria-hidden /> Reject
              </Button>
            </div>
            <InlineError error={decide.error} />
          </div>
        ) : (
          <p className="mt-3 text-xs text-muted-foreground">Viewers cannot decide approvals.</p>
        )
      ) : null}
    </Card>
  );
}
