"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, ListChecks, Wand2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useMemo, useState } from "react";

import { DecisionBadge } from "@/components/autopilot/decision";
import { DiffView } from "@/components/autopilot/diff-view";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/inputs";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { api, idempotencyKey, unwrap } from "@/lib/api";
import { useCandidates, useDatasets, useFailures, useVersions } from "@/lib/autopilot";
import { canWrite, useAuth } from "@/lib/auth";
import { formatTime, relativeTime } from "@/lib/format";
import { useAgents } from "@/lib/queries";
import type { AgentVersion, Candidate } from "@/types/autopilot";

type Diff = { field: string; before?: unknown; after?: unknown; added?: string[]; removed?: string[] };

function diffVersions(parent: AgentVersion | undefined, v: AgentVersion): Diff[] {
  if (!parent) return [];
  const a = parent.config_snapshot as Record<string, unknown>, b = v.config_snapshot as Record<string, unknown>;
  const out: Diff[] = [];
  for (const k of [...new Set([...Object.keys(a), ...Object.keys(b)])].sort()) {
    if (JSON.stringify(a[k]) !== JSON.stringify(b[k])) out.push({ field: k, before: a[k], after: b[k] });
  }
  return out;
}

export function Improvements() {
  const params = useSearchParams();
  const router = useRouter();
  const agents = useAgents();
  const agentId = params.get("agent") ?? agents.data?.items.find((a) => a.name === "Extraction Agent")?.id ?? agents.data?.items[0]?.id ?? null;
  const cands = useCandidates(agentId ?? undefined);
  const versions = useVersions(agentId);
  const failures = useFailures(24 * 30);
  const { role } = useAuth();
  const qc = useQueryClient();
  const [experimentFor, setExperimentFor] = useState<Candidate | null>(null);
  const diagnoses = useMemo(() => (failures.data?.diagnoses ?? []).filter((d) => d.agent_id === agentId && d.status === "hypothesis"), [failures.data, agentId]);
  const [diagnosisId, setDiagnosisId] = useState<string>("");
  const generate = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/autopilot/candidates/generate", {
      params: { header: { "Idempotency-Key": idempotencyKey() } },
      body: { agent_id: agentId!, diagnosis_id: diagnosisId || diagnoses[0]?.id || null, generator: "model", window_days: 30 } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["autopilot"] }),
  });

  const generateRules = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/autopilot/candidates/generate", {
      params: { header: { "Idempotency-Key": idempotencyKey() } },
      body: { agent_id: agentId!, diagnosis_id: diagnosisId || null, generator: "rules", window_days: 30 } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["autopilot"] }),
  });
  const rules = useQuery({ queryKey: ["autopilot", "rules"], queryFn: async () => unwrap(await api.GET("/api/v1/autopilot/rules")) as unknown as { version: string; rules: { id: string; version: string; candidate_type: string; symptom: string; action: string; threshold: string }[] } });

  if (agents.isLoading) return <LoadingBlock rows={6} />;
  if (agents.error) return <ErrorState error={agents.error} />;
  const writer = canWrite(role);
  const vById = new Map((versions.data ?? []).map((v) => [v.id, v]));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="w-72">
          <Field label="Agent" htmlFor="agent-pick">
            <Select id="agent-pick" value={agentId ?? ""} onChange={(e) => router.replace(`/autopilot/improvements?agent=${e.target.value}`)}>
              {agents.data?.items.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
            </Select>
          </Field>
        </div>
        {writer ? (
          <div className="flex flex-wrap items-end gap-2">
            <div className="w-96">
              <Field label="From diagnosis" htmlFor="diag-pick">
                <Select id="diag-pick" value={diagnosisId} onChange={(e) => setDiagnosisId(e.target.value)}>
                  <option value="">{diagnoses.length ? "Latest hypothesis" : "No diagnosis (generic proposals)"}</option>
                  {diagnoses.map((d) => <option key={d.id} value={d.id}>{d.failure_category}: {d.hypothesis.slice(0, 60)}…</option>)}
                </Select>
              </Field>
            </div>
            <Button onClick={() => generate.mutate()} disabled={!agentId || generate.isPending} data-testid="generate-candidates">
              <Wand2 aria-hidden /> {generate.isPending ? "Generating…" : "Generate candidates"}
            </Button>
            <Button variant="outline" onClick={() => generateRules.mutate()} disabled={!agentId || generateRules.isPending} data-testid="generate-rule-candidates">
              <ListChecks aria-hidden /> {generateRules.isPending ? "Reading traces…" : "Generate from trace rules"}
            </Button>
          </div>
        ) : null}
      </div>
      <InlineError error={generate.error ?? generateRules.error} />
      {generateRules.isSuccess && (generateRules.data as unknown as unknown[]).length === 0 ? (
        <p className="text-sm text-muted-foreground" data-testid="no-rule-match">No rule matched this agent&apos;s recent traces (or the matching proposals are already open). Rules stay silent without evidence.</p>
      ) : null}
      {rules.data ? (
        <details className="rounded-md border p-3 text-sm" data-testid="rule-catalog">
          <summary className="cursor-pointer font-medium">Rule set {rules.data.version}: {rules.data.rules.length} deterministic rules</summary>
          <Table className="mt-2"><THead><tr><TH>Rule</TH><TH>Type</TH><TH>Symptom</TH><TH>Action</TH><TH>Trigger</TH></tr></THead>
            <TBody>{rules.data.rules.map((r) => <TR key={r.id}><TD className="font-mono text-xs">{r.id} v{r.version}</TD><TD className="text-xs">{r.candidate_type}</TD><TD className="text-xs">{r.symptom}</TD><TD className="text-xs">{r.action}</TD><TD className="text-xs">{r.threshold}</TD></TR>)}</TBody></Table>
        </details>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="flex flex-col gap-3 xl:col-span-2">
          <h2 className="text-sm font-semibold">Candidates</h2>
          {cands.isLoading ? <LoadingBlock /> : cands.data?.items.length ? cands.data.items.map((c) => (
            <Card key={c.id} data-testid="candidate-card" data-status={c.status}>
              <CardHeader className="flex-row flex-wrap items-start justify-between gap-2">
                <div>
                  <CardTitle>{c.title}</CardTitle>
                  <CardDescription>
                    v{c.baseline_version} → {c.candidate_version ? `v${c.candidate_version}` : "no version (rejected)"} · {c.generator}
                    {c.simulated ? " · simulated optimizer" : c.provider ? ` · ${c.provider}/${c.model}` : ""} · {relativeTime(c.created_at)}
                    {c.rule_id ? <span data-testid="rule-badge"> · rule {c.rule_id} v{c.rule_version} ({c.candidate_type?.replace(/_/g, " ")}){c.projected ? " · projected saving, unverified" : ""}</span> : null}
                  </CardDescription>
                </div>
                <div className="flex items-center gap-2">
                  <DecisionBadge value={c.status} />
                  {c.latest_experiment_id ? (
                    <Button asChild size="sm" variant="outline"><Link href={`/autopilot/experiments/${c.latest_experiment_id}`}>View experiment</Link></Button>
                  ) : null}
                  {writer && c.candidate_version_id && c.status === "proposed" ? (
                    <Button size="sm" onClick={() => setExperimentFor(c)} data-testid="run-experiment"><FlaskConical aria-hidden /> Run experiment</Button>
                  ) : null}
                </div>
              </CardHeader>
              <CardContent className="flex flex-col gap-3 text-sm">
                {c.policy_violations.length ? (
                  <div className="rounded-md border border-danger/30 bg-danger/5 p-3" data-testid="policy-violations">
                    <p className="mb-1 text-xs font-medium text-danger">Rejected by policy (enforced in code)</p>
                    <ul className="list-disc pl-5 text-xs">{c.policy_violations.map((v, i) => <li key={i}>{String(v)}</li>)}</ul>
                  </div>
                ) : null}
                <div className="grid gap-3 md:grid-cols-2">
                  <div><p className="text-xs font-medium text-muted-foreground">Expected benefits</p>
                    <ul className="list-disc pl-5 text-xs">{c.expected_benefits.map((b, i) => <li key={i}>{String(b)}</li>)}</ul></div>
                  <div><p className="text-xs font-medium text-muted-foreground">Risks</p>
                    <ul className="list-disc pl-5 text-xs">{c.risk_assessment.map((b, i) => <li key={i}>{String(b)}</li>)}</ul></div>
                </div>
                <p className="text-xs text-muted-foreground">Reason: {c.reason || "—"}</p>
                {c.evidence?.length ? <p className="text-xs text-muted-foreground" data-testid="rule-evidence">Evidence: {c.evidence.length} trace records ({Array.from(new Set(c.evidence.map((e) => String((e as { type?: string }).type)))).join(", ")}), e.g. {String((c.evidence[0] as { id?: string }).id).slice(0, 8)}…</p> : null}
                {c.diff.length ? <details><summary className="cursor-pointer text-xs text-muted-foreground">Configuration diff</summary>
                  <div className="mt-2"><DiffView diff={c.diff as Diff[]} /></div></details> : null}
              </CardContent>
            </Card>
          )) : <EmptyState title="No candidates yet" description="Diagnose a failure cluster, then generate candidates." />}
        </div>
        <Card>
          <CardHeader><CardTitle>Version history</CardTitle><CardDescription>Immutable, content-addressed configurations</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-3" data-testid="version-history">
            {(versions.data ?? []).map((v) => (
              <details key={v.id} className="rounded-md border p-2 text-sm" open={v.is_active}>
                <summary className="flex cursor-pointer items-center gap-2">
                  <span className="font-semibold">v{v.version}</span>
                  {v.is_active ? <Badge tone="success">active</Badge> : null}
                  {v.is_canary ? <Badge tone="warning">canary</Badge> : null}
                  <Badge>{v.source}</Badge>
                  <span className="ml-auto text-xs text-muted-foreground">{formatTime(v.created_at)}</span>
                </summary>
                <p className="mt-1 text-xs text-muted-foreground">{v.change_summary}</p>
                <p className="font-mono text-[11px] text-muted-foreground">hash {v.config_hash.slice(0, 16)}… {v.parent_version_id ? `← v${vById.get(v.parent_version_id)?.version}` : ""}</p>
                {v.parent_version_id ? <div className="mt-2"><DiffView diff={diffVersions(vById.get(v.parent_version_id), v)} /></div> : null}
              </details>
            ))}
          </CardContent>
        </Card>
      </div>
      {experimentFor ? <ExperimentDialog candidate={experimentFor} onClose={() => setExperimentFor(null)} /> : null}
    </div>
  );
}

function ExperimentDialog({ candidate, onClose }: { candidate: Candidate; onClose: () => void }) {
  const datasets = useDatasets();
  const router = useRouter();
  const options = (datasets.data?.items ?? []).filter((d) => d.split !== "development");
  // The default is a visible choice, not a hidden one: the first validation dataset outside the Agent Arena corpus.
  const preferred = options.find((d) => d.split === "validation" && !d.name.startsWith("arena-")) ?? options.find((d) => d.split === "validation") ?? options[0];
  const [chosen, setChosen] = useState<string>("");
  const datasetId = chosen || preferred?.id || "";
  const [trials, setTrials] = useState(1);
  const [confirm, setConfirm] = useState(false);
  const start = useMutation({
    mutationFn: async () => unwrap(await api.POST("/api/v1/experiments", {
      params: { header: { "Idempotency-Key": idempotencyKey() } },
      body: { agent_id: candidate.agent_id, candidate_id: candidate.id,
              dataset_id: datasetId, trials, confirm_cost: confirm } })),
    onSuccess: (e) => router.push(`/autopilot/experiments/${e.id}`),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title="Run controlled experiment" description="Baseline and candidate run on the same cases, interleaved. Development data is not allowed (it was visible to the optimizer).">
        <Field label="Dataset (validation or held-out test)" htmlFor="exp-ds">
          <Select id="exp-ds" value={datasetId} onChange={(e) => setChosen(e.target.value)}>
            {options.map((d) => <option key={d.id} value={d.id}>{d.name} · {d.split} v{d.version} ({d.case_count} cases)</option>)}
          </Select>
        </Field>
        <Field label="Trials per case" htmlFor="exp-trials" hint="Use >1 with real models to account for nondeterminism.">
          <Input id="exp-trials" type="number" min={1} max={5} value={trials} onChange={(e) => setTrials(Number(e.target.value))} />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
          I confirm the estimated cost if a paid provider is used
        </label>
        <InlineError error={start.error} />
        <div className="flex justify-end">
          <Button onClick={() => start.mutate()} disabled={start.isPending || !datasetId} data-testid="confirm-experiment">{start.isPending ? "Starting…" : datasetId ? "Start experiment" : "Loading datasets…"}</Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
