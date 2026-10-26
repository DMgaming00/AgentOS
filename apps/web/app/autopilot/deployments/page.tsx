"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, RotateCcw, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { DecisionBadge } from "@/components/autopilot/decision";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Select, Textarea } from "@/components/ui/inputs";
import { KV } from "@/components/ui/json";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { pct, useDeploymentHealth, useDeploymentHistory, useDeploymentRequests, useVersions } from "@/lib/autopilot";
import { useAuth } from "@/lib/auth";
import { formatTime, relativeTime, shortId } from "@/lib/format";
import type { Deployment, DeploymentRequest } from "@/types/autopilot";

export default function DeploymentsPage() {
  const pending = useDeploymentRequests("pending");
  const decided = useDeploymentRequests("approved,rejected,expired");
  const history = useDeploymentHistory();
  const { role } = useAuth();
  if (pending.isLoading || history.isLoading) return <LoadingBlock rows={6} />;
  if (pending.error || history.error) return <ErrorState error={pending.error ?? history.error} />;
  const active = (history.data?.items ?? []).filter((d) => d.status === "active" && d.deployment_type !== "initial");
  const regressions = (history.data?.items ?? []).filter((d) => d.rollout_events.some((e) => e.type === "regression_detected"));
  const agents = [...new Map((history.data?.items ?? []).map((d) => [d.agent_id, d.agent_name])).entries()];
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h2 className="text-lg font-semibold">Safe deployments</h2>
        <p className="text-sm text-muted-foreground">Only eligible experiments can be promoted; an administrator must approve. Runs keep the version they started with.</p>
      </div>
      {regressions.filter((d) => d.status === "active").map((d) => (
        <div key={d.id} role="alert" className="flex items-start gap-3 rounded-lg border border-danger/40 bg-danger/5 p-4 text-sm" data-testid="regression-alert">
          <AlertTriangle className="mt-0.5 size-4 text-danger" aria-hidden />
          <div>
            <p className="font-medium text-danger">Live regression detected: {d.agent_name} v{d.version}</p>
            <p className="text-muted-foreground">{regressionText(d)}. Consider rolling back to v{d.previous_version}.</p>
          </div>
        </div>
      ))}
      <section className="flex flex-col gap-3" aria-labelledby="pending-deploys">
        <h3 id="pending-deploys" className="text-sm font-semibold">Pending approval ({pending.data?.total ?? 0})</h3>
        {pending.data?.items.length ? pending.data.items.map((r) => <RequestCard key={r.id} req={r} isAdmin={role === "admin"} />)
          : <EmptyState title="No promotion requests" description="Request one from an eligible experiment." />}
      </section>
      <div className="grid gap-4 xl:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Live health</CardTitle><CardDescription>Failure rate of live runs on the deployed version vs its predecessor</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-3">
            {active.length ? active.map((d) => <Health key={d.id} dep={d} />) : <p className="text-sm text-muted-foreground">No monitored deployments.</p>}
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Rollback</CardTitle><CardDescription>Restores a previously deployed version for new runs. Admin only.</CardDescription></CardHeader>
          <CardContent>
            {role === "admin" ? agents.map(([id, name]) => <RollbackForm key={id} agentId={id} name={name ?? ""} />)
              : <p className="text-sm text-muted-foreground">Only administrators can roll back.</p>}
          </CardContent>
        </Card>
      </div>
      <Card>
        <CardHeader><CardTitle>Deployment history</CardTitle><CardDescription>Immutable records; rollbacks are new records</CardDescription></CardHeader>
        <CardContent>
          <Table data-testid="deployment-history">
            <THead><tr><TH>When</TH><TH>Agent</TH><TH>Type</TH><TH>Version</TH><TH>Status</TH><TH>Approved by</TH><TH>Reason</TH><TH>Experiment</TH><TH>Events</TH></tr></THead>
            <TBody>{history.data?.items.map((d) => (
              <TR key={d.id} data-type={d.deployment_type}>
                <TD className="text-muted-foreground">{formatTime(d.deployed_at)}</TD><TD>{d.agent_name}</TD>
                <TD><Badge>{d.deployment_type}{d.deployment_type === "canary" ? ` ${d.canary_percent}%` : ""}</Badge></TD>
                <TD>{d.previous_version ? `v${d.previous_version} → ` : ""}v{d.version}</TD>
                <TD><DecisionBadge value={d.status} /></TD>
                <TD className="text-xs">{d.approved_by_email ?? "—"}</TD>
                <TD className="max-w-64 truncate text-xs" title={d.reason}>{d.reason}</TD>
                <TD>{d.experiment_id ? <Link className="font-mono text-xs text-primary hover:underline" href={`/autopilot/experiments/${d.experiment_id}`}>{shortId(d.experiment_id)}</Link> : "—"}</TD>
                <TD className="text-xs">{d.rollout_events.map((e) => e.type).join(", ") || "—"}</TD>
              </TR>))}</TBody>
          </Table>
        </CardContent>
      </Card>
      {decided.data?.items.length ? (
        <Card>
          <CardHeader><CardTitle>Decided requests</CardTitle></CardHeader>
          <CardContent>
            <Table><THead><tr><TH>Agent</TH><TH>Change</TH><TH>Status</TH><TH>Decided</TH><TH>Note</TH></tr></THead>
              <TBody>{decided.data.items.map((r) => (
                <TR key={r.id}><TD>{r.agent_name}</TD><TD>v{r.baseline_version} → v{r.candidate_version} ({r.mode})</TD><TD><DecisionBadge value={r.status} /></TD>
                  <TD className="text-muted-foreground">{relativeTime(r.decided_at)}</TD><TD className="text-xs">{r.decision_reason ?? "—"}</TD></TR>))}</TBody></Table>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

function regressionText(d: Deployment): string {
  const e = d.rollout_events.find((x) => x.type === "regression_detected")?.payload as
    { current?: { failure_rate: number; n: number }; previous?: { failure_rate: number; n: number } } | undefined;
  if (!e?.current) return "failure rate increased";
  return `failure rate ${pct(e.previous?.failure_rate)} (n=${e.previous?.n}) → ${pct(e.current.failure_rate)} (n=${e.current.n})`;
}

function RequestCard({ req, isAdmin }: { req: DeploymentRequest; isAdmin: boolean }) {
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const decide = useMutation({
    mutationFn: async (approve: boolean) => {
      const path = approve ? "/api/v1/deployments/requests/{request_id}/approve" : "/api/v1/deployments/requests/{request_id}/reject";
      return unwrap(await api.POST(path, { params: { path: { request_id: req.id }, header: { "Idempotency-Key": idempotencyKey() } },
                                          body: { reason: reason || null } }));
    },
    onSettled: () => void qc.invalidateQueries({ queryKey: ["autopilot"] }),
  });
  return (
    <Card className="p-4" data-testid="deploy-request">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="font-medium">{req.agent_name}: v{req.baseline_version} → v{req.candidate_version} ({req.mode}{req.mode === "canary" ? ` ${req.canary_percent}%` : ""})</p>
          <p className="text-xs text-muted-foreground">
            <Link href={`/autopilot/experiments/${req.experiment_id}`} className="text-primary hover:underline">Review experiment evidence and diff</Link>
          </p>
        </div>
        <DecisionBadge value={req.status} />
      </div>
      <div className="mt-2"><KV items={[["Requested", formatTime(req.requested_at)], ["Expires", formatTime(req.expires_at)], ["Reason", req.reason || "—"]]} /></div>
      {isAdmin ? (
        <div className="mt-3 flex flex-col gap-2">
          <label htmlFor={`dr-${req.id}`} className="sr-only">Decision note</label>
          <Textarea id={`dr-${req.id}`} className="min-h-12" placeholder="Decision note" value={reason} onChange={(e) => setReason(e.target.value)} />
          <div className="flex gap-2">
            <Button variant="success" onClick={() => decide.mutate(true)} disabled={decide.isPending} data-testid="approve-deploy"><Check aria-hidden /> Approve and deploy</Button>
            <Button variant="destructive" onClick={() => decide.mutate(false)} disabled={decide.isPending}><X aria-hidden /> Reject</Button>
          </div>
          <InlineError error={decide.error} />
        </div>
      ) : <p className="mt-2 text-xs text-muted-foreground">Waiting for an administrator.</p>}
    </Card>
  );
}

function Health({ dep }: { dep: Deployment }) {
  const { data } = useDeploymentHealth(dep.id);
  if (!data) return <LoadingBlock rows={1} />;
  return (
    <div className="rounded-md border p-3 text-sm" data-testid="deploy-health">
      <p className="flex items-center gap-2 font-medium">{dep.agent_name} v{dep.version}
        {data.regression ? <Badge tone="danger">regression</Badge> : <Badge tone="success">no regression flagged</Badge>}</p>
      <p className="text-xs text-muted-foreground">
        live failure rate {pct(data.current.failure_rate)} ({data.current.failed}/{data.current.n}) vs previous v{dep.previous_version ?? "—"}{" "}
        {data.previous ? `${pct(data.previous.failure_rate)} (${data.previous.failed}/${data.previous.n})` : "n/a"}
        {data.note ? ` · ${data.note}` : ""}
      </p>
    </div>
  );
}

function RollbackForm({ agentId, name }: { agentId: string; name: string }) {
  const versions = useVersions(agentId);
  const qc = useQueryClient();
  const [reason, setReason] = useState("");
  const [target, setTarget] = useState("");
  const rb = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/deployments/rollback", {
      params: { header: { "Idempotency-Key": idempotencyKey() } },
      body: { agent_id: agentId, reason, target_version_id: target || null } })),
    onSuccess: () => { setReason(""); void qc.invalidateQueries({ queryKey: ["autopilot"] }); void qc.invalidateQueries({ queryKey: ["agents"] }); },
  });
  const active = versions.data?.find((v) => v.is_active);
  return (
    <div className="mb-3 flex flex-col gap-2 rounded-md border p-3" data-testid={`rollback-${name}`}>
      <p className="text-sm font-medium">{name} <span className="text-xs text-muted-foreground">(active v{active?.version ?? "—"})</span></p>
      <div className="flex flex-wrap gap-2">
        <label htmlFor={`rt-${agentId}`} className="sr-only">Target version</label>
        <Select id={`rt-${agentId}`} className="w-48" value={target} onChange={(e) => setTarget(e.target.value)}>
          <option value="">Previous version</option>
          {versions.data?.filter((v) => !v.is_active).map((v) => <option key={v.id} value={v.id}>v{v.version} ({v.source})</option>)}
        </Select>
        <label htmlFor={`rr-${agentId}`} className="sr-only">Reason</label>
        <input id={`rr-${agentId}`} className="h-9 flex-1 rounded-md border border-input bg-card px-3 text-sm" placeholder="Reason (required)"
               value={reason} onChange={(e) => setReason(e.target.value)} />
        <Button variant="destructive" onClick={() => rb.mutate()} disabled={rb.isPending || reason.trim().length < 3} data-testid="rollback-button">
          <RotateCcw aria-hidden /> Roll back
        </Button>
      </div>
      <InlineError error={rb.error} />
    </div>
  );
}
