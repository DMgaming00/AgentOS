"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { Plus, Trash2, Zap } from "lucide-react";
import { useMemo, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/inputs";
import { StatusBadge } from "@/components/ui/status";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useAuth } from "@/lib/auth";
import { useChaosCatalog, useExperiments, useScenarios, useStartExperiment } from "@/lib/chaos";
import { formatTime, shortId } from "@/lib/format";
import { useWorkflows } from "@/lib/queries";
import type { ChaosPlan, FaultSpec } from "@/types/chaos";

const DEFAULT_PLAN: ChaosPlan = {
  seed: 1, trials: 10, max_affected_runs: 10, duration_seconds: 120, recovery_policy: "as_configured", approver: "auto_approve", safety_budget_usd: 1,
  faults: [{ type: "provider_timeout", probability: 0.3, max_per_step: 1 }],
};

export function ChaosHome() {
  const router = useRouter();
  const { role } = useAuth();
  const canWrite = role === "admin" || role === "developer";
  const catalog = useChaosCatalog();
  const scenarios = useScenarios();
  const experiments = useExperiments();
  const workflows = useWorkflows();
  const start = useStartExperiment();
  const [name, setName] = useState("Provider brownout");
  const [workflowId, setWorkflowId] = useState("");
  const [scenario, setScenario] = useState("provider-brownout");
  const [plan, setPlan] = useState<ChaosPlan>(DEFAULT_PLAN);
  const [picked, setPicked] = useState<string[]>([]);

  const faults = useMemo(() => new Map((catalog.data?.faults ?? []).map((f) => [f.type, f])), [catalog.data]);
  const loadScenario = (n: string) => {
    setScenario(n);
    const s = scenarios.data?.find((x) => x.name === n);
    if (s) {
      setPlan({ ...DEFAULT_PLAN, ...s.plan });
      setName(s.name.replace(/-/g, " "));
    }
  };
  const setFault = (i: number, patch: Partial<FaultSpec>) => setPlan((p) => ({ ...p, faults: p.faults.map((f, j) => (j === i ? { ...f, ...patch } : f)) }));
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p.slice(-1), id]));

  if (catalog.isLoading || workflows.isLoading || scenarios.isLoading) return <LoadingBlock rows={6} />;
  if (catalog.error || workflows.error) return <ErrorState error={catalog.error ?? workflows.error} />;
  const wfItems = workflows.data?.items ?? [];
  const exps = experiments.data?.items ?? [];
  const selectedWorkflow = workflowId || wfItems[0]?.id || "";

  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Chaos Lab</h1>
        <p className="text-sm text-muted-foreground">
          Seeded, bounded fault injection at the platform&apos;s real boundaries: provider calls, tools, worker processes, Redis, PostgreSQL, budgets and approvals.
          The same plan and seed reproduce the same fault schedule. Approvals in experiments are decided by a <Badge tone="warning">simulated reviewer</Badge>.
        </p>
      </div>

      {canWrite ? (
        <Card data-testid="experiment-editor">
          <CardHeader><CardTitle>New experiment</CardTitle><CardDescription>Faults fire only for this experiment&apos;s runs, only inside its time window, and never beyond the limits below.</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="grid gap-3 md:grid-cols-3">
              <Field label="Name" htmlFor="exp-name"><Input id="exp-name" value={name} onChange={(e) => setName(e.target.value)} /></Field>
              <Field label="Target workflow" htmlFor="exp-wf">
                <Select id="exp-wf" value={selectedWorkflow} onChange={(e) => setWorkflowId(e.target.value)}>
                  {wfItems.map((w) => <option key={w.id} value={w.id}>{w.name} v{w.version}</option>)}
                </Select>
              </Field>
              <Field label="Start from scenario" htmlFor="exp-scenario">
                <Select id="exp-scenario" value={scenario} onChange={(e) => loadScenario(e.target.value)}>
                  {scenarios.data?.map((s) => <option key={s.name} value={s.name}>{s.name}{s.builtin ? "" : " (saved)"}</option>)}
                </Select>
              </Field>
            </div>
            <p className="text-xs text-muted-foreground">{scenarios.data?.find((s) => s.name === scenario)?.description}</p>
            <div className="grid gap-3 md:grid-cols-4 lg:grid-cols-7">
              <Field label="Seed" htmlFor="p-seed"><Input id="p-seed" type="number" value={plan.seed} onChange={(e) => setPlan({ ...plan, seed: Number(e.target.value) })} /></Field>
              <Field label={`Runs (max ${catalog.data?.limits.max_runs})`} htmlFor="p-trials"><Input id="p-trials" type="number" value={plan.trials} onChange={(e) => setPlan({ ...plan, trials: Number(e.target.value), max_affected_runs: Math.min(plan.max_affected_runs, Number(e.target.value)) })} /></Field>
              <Field label="Max affected runs" htmlFor="p-aff"><Input id="p-aff" type="number" value={plan.max_affected_runs} onChange={(e) => setPlan({ ...plan, max_affected_runs: Number(e.target.value) })} /></Field>
              <Field label="Duration (s)" htmlFor="p-dur"><Input id="p-dur" type="number" value={plan.duration_seconds} onChange={(e) => setPlan({ ...plan, duration_seconds: Number(e.target.value) })} /></Field>
              <Field label="Recovery policy" htmlFor="p-rec" hint="as configured / no retries / 5 attempts">
                <Select id="p-rec" value={plan.recovery_policy} onChange={(e) => setPlan({ ...plan, recovery_policy: e.target.value as ChaosPlan["recovery_policy"] })}>
                  <option value="as_configured">as configured</option><option value="no_retry">no retry</option><option value="extra_retries">extra retries</option>
                </Select>
              </Field>
              <Field label="Reviewer" htmlFor="p-app"><Select id="p-app" value={plan.approver} onChange={(e) => setPlan({ ...plan, approver: e.target.value as ChaosPlan["approver"] })}><option value="auto_approve">simulated: approve</option><option value="none">none</option></Select></Field>
              <Field label="Safety budget ($)" htmlFor="p-bud" hint="estimated; cancels when exceeded"><Input id="p-bud" type="number" step="0.1" value={plan.safety_budget_usd} onChange={(e) => setPlan({ ...plan, safety_budget_usd: Number(e.target.value) })} /></Field>
            </div>
            <div className="flex flex-col gap-2" data-testid="fault-list">
              <p className="text-sm font-medium">Faults</p>
              {plan.faults.map((f, i) => {
                const info = faults.get(f.type);
                return (
                  <div key={i} className="grid items-end gap-2 rounded-md border p-2 md:grid-cols-[2fr_1.4fr_1fr_1fr_auto]" data-testid="fault-row">
                    <Field label="Fault" htmlFor={`f-type-${i}`} hint={info?.description}>
                      <Select id={`f-type-${i}`} value={f.type} onChange={(e) => setFault(i, { type: e.target.value, point: faults.get(e.target.value)?.default_point })}>
                        {catalog.data?.faults.map((x) => <option key={x.type} value={x.type}>{x.type}</option>)}
                      </Select>
                    </Field>
                    <Field label="Injection point" htmlFor={`f-point-${i}`}>
                      <Select id={`f-point-${i}`} value={f.point ?? info?.default_point} onChange={(e) => setFault(i, { point: e.target.value })}>
                        {info?.points.map((p) => <option key={p} value={p}>{p}</option>)}
                      </Select>
                    </Field>
                    <Field label={`Probability ${Math.round(f.probability * 100)}%`} htmlFor={`f-p-${i}`}>
                      <input id={`f-p-${i}`} type="range" min={0} max={1} step={0.05} value={f.probability} onChange={(e) => setFault(i, { probability: Number(e.target.value) })} className="accent-primary" />
                    </Field>
                    <Field label="Max per step" htmlFor={`f-m-${i}`}><Input id={`f-m-${i}`} type="number" min={1} max={5} value={f.max_per_step ?? 1} onChange={(e) => setFault(i, { max_per_step: Number(e.target.value) })} /></Field>
                    <Button variant="ghost" size="icon" aria-label="Remove fault" onClick={() => setPlan({ ...plan, faults: plan.faults.filter((_, j) => j !== i) })} disabled={plan.faults.length === 1}><Trash2 aria-hidden /></Button>
                    {info ? <p className="text-xs text-muted-foreground md:col-span-5">Expected platform response: {info.expected_platform_response}</p> : null}
                  </div>
                );
              })}
              <Button variant="outline" size="sm" className="self-start" onClick={() => setPlan({ ...plan, faults: [...plan.faults, { type: "tool_failure", probability: 0.2, max_per_step: 1 }] })} disabled={plan.faults.length >= 8}>
                <Plus aria-hidden /> Add fault
              </Button>
            </div>
            <div className="flex items-center gap-3">
              <Button data-testid="start-experiment" disabled={start.isPending || !selectedWorkflow} onClick={() => start.mutate({ name, workflow_id: selectedWorkflow, plan }, { onSuccess: (e) => router.push(`/chaos/${e.id}`) })}>
                <Zap aria-hidden /> Run experiment
              </Button>
              <span className="text-xs text-muted-foreground">Offline mode: no paid provider is ever called; costs are simulated.</span>
            </div>
            <InlineError error={start.error} />
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <div><CardTitle>Experiments</CardTitle><CardDescription>Select two completed experiments with the same plan to compare reliability before and after a change.</CardDescription></div>
          <Button size="sm" disabled={picked.length !== 2} data-testid="compare-experiments" onClick={() => router.push(`/chaos/compare?a=${picked[0]}&b=${picked[1]}`)}>Compare ({picked.length}/2)</Button>
        </CardHeader>
        <CardContent>
          {exps.length ? (
            <Table data-testid="experiments-table">
              <THead><tr><TH /><TH>Experiment</TH><TH>Status</TH><TH>Faults</TH><TH>Runs</TH><TH>Completed</TH><TH>Injections</TH><TH>Started</TH></tr></THead>
              <TBody>
                {exps.map((e) => (
                  <TR key={e.id}>
                    <TD><input type="checkbox" aria-label={`Select ${e.name} for comparison`} checked={picked.includes(e.id)} onChange={() => toggle(e.id)} disabled={e.status !== "completed"} /></TD>
                    <TD><Link className="text-primary hover:underline" href={`/chaos/${e.id}`}>{e.name}</Link> <span className="font-mono text-xs text-muted-foreground">{shortId(e.id)}</span></TD>
                    <TD><StatusBadge status={e.status === "completed" ? "succeeded" : e.status === "cancelled" ? "cancelled" : e.status === "failed" ? "failed" : "running"} /></TD>
                    <TD className="text-xs">{[...new Set(e.faults)].join(", ")}</TD>
                    <TD>{e.trials}</TD>
                    <TD>{e.completion_rate == null ? "…" : `${Math.round(e.completion_rate * 100)}%`}</TD>
                    <TD>{e.injections_total ?? "…"}</TD>
                    <TD className="text-muted-foreground">{formatTime(e.started_at)}</TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          ) : <EmptyState title="No experiments yet" description="Pick a scenario above and run it." />}
        </CardContent>
      </Card>
    </div>
  );
}
