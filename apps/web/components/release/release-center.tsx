"use client";

import { CheckCircle2, CircleDashed, Rocket, Undo2, XCircle } from "lucide-react";
import { useState } from "react";

import { DiffView } from "@/components/autopilot/diff-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Input, Select } from "@/components/ui/inputs";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useExperiments } from "@/lib/autopilot";
import { useAuth } from "@/lib/auth";
import { useAgents } from "@/lib/queries";
import { useEnvironments, useGatePreview, useRelease, useReleaseMutations, useReleases } from "@/lib/release";
import type { Gate, Plan, RuleResult } from "@/types/release";

const STATUS_TONE = { pending_approval: "warning", active: "info", completed: "success", rolled_back: "danger", aborted: "neutral" } as const;
const RULE_TONE = { triggered: "danger", ok: "success", insufficient_evidence: "warning", disabled: "neutral" } as const;

function GateRow({ g }: { g: Gate }) {
  const Icon = g.passed === true ? CheckCircle2 : g.passed === false ? XCircle : CircleDashed;
  const tone = g.passed === true ? "text-success" : g.passed === false ? "text-danger" : "text-warning";
  return (
    <li className="flex items-start gap-2 text-sm" data-gate={g.gate} data-passed={String(g.passed)}>
      <Icon className={`mt-0.5 size-4 shrink-0 ${tone}`} aria-hidden />
      <span><span className="font-medium">{g.gate.replace(/_/g, " ")}</span><span className="block text-xs text-muted-foreground">{g.detail}</span></span>
    </li>
  );
}

function Ladder({ plan }: { plan: Plan }) {
  const done = plan.status === "completed";
  return (
    <ol className="flex flex-wrap items-center gap-2" data-testid="stage-ladder" aria-label="Canary stages">
      {plan.schedule.map((p, i) => {
        const state = done || i < plan.stage_index ? "done" : i === plan.stage_index && plan.status === "active" ? "current" : "todo";
        return (
          <li key={p} data-stage={p} data-state={state} className={`rounded-md border px-3 py-1 text-sm ${state === "current" ? "border-primary bg-primary/10 font-semibold" : state === "done" ? "border-success/40 bg-success/10" : "text-muted-foreground"}`}>
            {p}%{p === 100 ? " (promote)" : ""}
          </li>
        );
      })}
    </ol>
  );
}

function Detail({ id, admin }: { id: string; admin: boolean }) {
  const q = useRelease(id);
  const m = useReleaseMutations();
  const [reason, setReason] = useState("");
  if (q.isLoading) return <LoadingBlock rows={4} />;
  if (q.error) return <ErrorState error={q.error} />;
  const p = q.data!;
  const live = p.status === "active";
  return (
    <div className="flex flex-col gap-4" data-testid="release-detail" data-status={p.status}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-lg font-semibold">{p.agent}: v{p.baseline_version} → v{p.candidate_version} <Badge tone={STATUS_TONE[p.status]} data-testid="release-status">{p.status.replace("_", " ")}</Badge></h3>
          <p className="text-xs text-muted-foreground">{p.environment}{p.environment === "production" ? " (production-style local simulation)" : ""} · seed <span className="font-mono">{p.seed}</span> · auto-rollback {p.auto_rollback ? "on" : "off"}{p.end_reason ? ` · ${p.end_reason}` : ""}</p>
        </div>
        {admin && live ? (
          <div className="flex items-center gap-2">
            <Button data-testid="advance-release" disabled={m.advance.isPending} onClick={() => m.advance.mutate(p.id)}><Rocket aria-hidden /> Advance stage</Button>
            <Button variant="outline" onClick={() => m.patchRules.mutate({ id: p.id, auto_rollback: !p.auto_rollback })}>{p.auto_rollback ? "Disable" : "Enable"} auto-rollback</Button>
          </div>) : null}
      </div>
      <InlineError error={m.advance.error ?? m.rollback.error ?? m.patchRules.error} />
      {p.environment === "production" ? <Ladder plan={p} /> : null}
      {p.approval ? <p className="text-xs text-muted-foreground" data-testid="approval-audit">Approval: {p.approval.status}{p.approval.decided_by ? ` by ${p.approval.decided_by.slice(0, 8)}` : ""}{p.approval.decided_at ? ` at ${new Date(p.approval.decided_at).toLocaleString()}` : ""}{p.approval.reason ? ` — ${p.approval.reason}` : ""}</p> : null}
      {p.evaluation ? (
        <div className="grid gap-4 xl:grid-cols-2">
          <Card data-testid="live-comparison">
            <CardHeader><CardTitle className="text-base">Live comparison: canary vs baseline</CardTitle><CardDescription>{p.evaluation.canary_steps} canary steps and {p.evaluation.baseline_steps} baseline steps since this stage began ({p.evaluation.percent}%). A rule needs its minimum evidence before it can fire.</CardDescription></CardHeader>
            <CardContent>
              <Table>
                <THead><tr><TH>Rollback rule</TH><TH>Status</TH><TH>Reason</TH></tr></THead>
                <TBody>{p.evaluation.rules.map((r: RuleResult) => <TR key={r.rule} data-rule={r.rule} data-status={r.status}><TD className="text-xs">{r.rule.replace(/_/g, " ")}</TD><TD><Badge tone={RULE_TONE[r.status]}>{r.status.replace("_", " ")}</Badge></TD><TD className="text-xs text-muted-foreground">{r.reason}</TD></TR>)}</TBody>
              </Table>
            </CardContent>
          </Card>
          <Card data-testid="stage-gates">
            <CardHeader><CardTitle className="text-base">Gates for the next stage</CardTitle></CardHeader>
            <CardContent><ul className="flex flex-col gap-2">{p.stage_gates?.map((g) => <GateRow key={g.gate} g={g} />)}</ul></CardContent>
          </Card>
        </div>) : null}
      {admin && live ? (
        <div className="flex items-end gap-2">
          <Input aria-label="Rollback reason" className="w-96" placeholder="Reason for rolling back" value={reason} onChange={(e) => setReason(e.target.value)} />
          <Button variant="outline" data-testid="rollback-release" disabled={reason.trim().length < 3 || m.rollback.isPending} onClick={() => m.rollback.mutate({ id: p.id, reason })}><Undo2 aria-hidden /> Roll back to v{p.baseline_version}</Button>
        </div>) : null}
      <details className="rounded-md border p-3 text-sm"><summary className="cursor-pointer font-medium">Configuration change in this release</summary><div className="mt-2"><DiffView diff={p.config_diff} /></div></details>
      <Card>
        <CardHeader><CardTitle className="text-base">Audit trail</CardTitle><CardDescription>Append-only: gates, approvals, stages, triggered rules and rollbacks.</CardDescription></CardHeader>
        <CardContent>
          <Table data-testid="release-events">
            <THead><tr><TH>When</TH><TH>Event</TH><TH>Actor</TH><TH>Detail</TH></tr></THead>
            <TBody>{p.events.map((e) => <TR key={e.id}><TD className="text-xs">{new Date(e.at).toLocaleTimeString()}</TD><TD><Badge tone={e.type === "rolled_back" || e.type === "advance_blocked" ? "danger" : "neutral"}>{e.type.replace(/_/g, " ")}</Badge></TD><TD className="text-xs">{e.actor?.slice(0, 8) ?? "system"}</TD><TD className="max-w-md truncate font-mono text-[11px]">{JSON.stringify(e.payload).slice(0, 160)}</TD></TR>)}</TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}

function NewRelease() {
  const agents = useAgents();
  const experiments = useExperiments();
  const m = useReleaseMutations();
  const [agentId, setAgentId] = useState("");
  const [expId, setExpId] = useState("");
  const [env, setEnv] = useState<"development" | "staging" | "production">("production");
  const [auto, setAuto] = useState(false);
  const [samples, setSamples] = useState(10);
  const [schedule, setSchedule] = useState("default");
  const eligible = (experiments.data?.items ?? []).filter((e) => e.agent_id === agentId && e.decision === "eligible" && e.status === "completed");
  const effectiveExp = expId || eligible[0]?.id || "";
  const preview = useGatePreview(env === "development" ? null : agentId || null, effectiveExp || null, env === "staging" ? "staging" : "production");
  const blocked = env !== "development" && (!preview.data || !preview.data.can_request);
  return (
    <Card>
      <CardHeader><CardTitle>New release</CardTitle><CardDescription>Development needs no gate; staging needs the gates; production needs the gates, an administrator&apos;s approval, then staged canary rollout (5 → 10 → 25 → 50 → 100%).</CardDescription></CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex flex-wrap items-end gap-2">
          <Select aria-label="Release agent" className="w-56" value={agentId} onChange={(e) => { setAgentId(e.target.value); setExpId(""); }}><option value="">Choose an agent…</option>{agents.data?.items.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</Select>
          <Select aria-label="Release experiment" className="w-72" value={effectiveExp} onChange={(e) => setExpId(e.target.value)}>{eligible.length === 0 ? <option value="">No eligible experiment for this agent</option> : eligible.map((e) => <option key={e.id} value={e.id}>{e.candidate_title ?? "candidate"} · v{e.candidate_version} · {new Date(e.created_at).toLocaleString()}</option>)}</Select>
          <Select aria-label="Release environment" className="w-44" value={env} onChange={(e) => setEnv(e.target.value as typeof env)}><option value="production">Production-style</option><option value="staging">Staging</option><option value="development">Development</option></Select>
          {env === "production" ? <><Select aria-label="Canary schedule" className="w-64" value={schedule} onChange={(e) => setSchedule(e.target.value)}><option value="default">5 → 10 → 25 → 50 → 100%</option><option value="fast">50 → 100% (short schedule)</option></Select><label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)} /> auto-rollback</label>
            <Input aria-label="Minimum samples per stage" type="number" min={1} className="w-28" value={samples} onChange={(e) => setSamples(Number(e.target.value))} /></> : null}
          <Button data-testid="create-release" disabled={!agentId || !effectiveExp && env !== "development" || blocked || m.create.isPending} onClick={() => m.create.mutate({ agent_id: agentId, environment: env, experiment_id: effectiveExp || undefined, auto_rollback: auto, min_stage_samples: samples, min_stage_seconds: 0, reason: "release from the Release Center", schedule: env === "production" && schedule === "fast" ? [50, 100] : undefined })}>Create release</Button>
        </div>
        <InlineError error={m.create.error} />
        {env !== "development" && agentId ? (
          <div data-testid="gate-preview">
            <p className="mb-1 text-xs font-medium text-muted-foreground">Gate check</p>
            {preview.isLoading ? <LoadingBlock rows={2} /> : <ul className="flex flex-col gap-2">{preview.data?.gates.map((g) => <GateRow key={g.gate} g={g} />)}</ul>}
          </div>) : null}
      </CardContent>
    </Card>
  );
}

export function ReleaseCenter() {
  const { role } = useAuth();
  const envs = useEnvironments();
  const releases = useReleases();
  const [picked, setPicked] = useState<string | null>(null);
  const canWrite = role === "admin" || role === "developer";
  const id = picked ?? releases.data?.[0]?.id ?? null;
  if (envs.isLoading) return <LoadingBlock rows={6} />;
  if (envs.error) return <ErrorState error={envs.error} onRetry={() => void envs.refetch()} />;
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold"><Rocket className="size-5" aria-hidden /> Release Center</h1>
        <p className="text-sm text-muted-foreground">Environments, gated staged canary rollouts with seeded routing, deterministic rollback rules and a full audit trail.</p>
      </div>
      <div role="note" className="rounded-lg border bg-card p-3 text-sm text-muted-foreground">
        <Badge tone="warning">local simulation</Badge> &quot;Production&quot; here is a production-style local environment inside this deployment (the agent&apos;s own active and canary versions). It is not a hosted production service. Runs are pinned to a version when they are created.
      </div>
      <Card>
        <CardHeader><CardTitle>Environments</CardTitle><CardDescription>Development and staging use their own version pin when set, otherwise the production active version.</CardDescription></CardHeader>
        <CardContent>
          <Table data-testid="environments">
            <THead><tr><TH>Agent</TH><TH>Development</TH><TH>Staging</TH><TH>Production-style</TH></tr></THead>
            <TBody>{envs.data!.items.map((a) => (
              <TR key={a.agent_id}><TD className="font-medium">{a.agent}</TD>
                {(["development", "staging"] as const).map((e) => <TD key={e} className="text-xs">{a.environments[e].version ? `v${a.environments[e].version!.version}` : <span className="text-muted-foreground">→ production</span>}</TD>)}
                <TD className="text-xs">v{a.environments.production.version?.version}{a.environments.production.canary ? <Badge tone="info" className="ml-2" data-testid="canary-badge">canary v{a.environments.production.canary.version} · {a.environments.production.canary_percent}%</Badge> : null}</TD></TR>))}</TBody>
          </Table>
        </CardContent>
      </Card>
      {canWrite ? <NewRelease /> : null}
      <div className="grid gap-4 xl:grid-cols-[320px_1fr]">
        <Card>
          <CardHeader><CardTitle>Releases</CardTitle></CardHeader>
          <CardContent className="flex flex-col gap-2" data-testid="release-list">
            {releases.data?.length ? releases.data.map((r) => (
              <button key={r.id} onClick={() => setPicked(r.id)} className={`rounded-md border p-2 text-left text-sm ${id === r.id ? "border-primary bg-primary/5" : ""}`} data-release-status={r.status}>
                <div className="flex items-center gap-2"><span className="font-medium">{r.agent}</span><Badge tone={STATUS_TONE[r.status]}>{r.status.replace("_", " ")}</Badge></div>
                <p className="text-xs text-muted-foreground">{r.environment} · {r.schedule.length > 1 ? `${r.percent}% of ${r.schedule.join("/")}` : "pinned"} · {new Date(r.created_at).toLocaleString()}</p>
              </button>)) : <EmptyState title="No releases yet" description={canWrite ? "Create one above." : "A developer can create releases."} />}
          </CardContent>
        </Card>
        <div>{id ? <Detail id={id} admin={role === "admin"} /> : null}</div>
      </div>
    </div>
  );
}
