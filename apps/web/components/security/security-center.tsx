"use client";

import Link from "next/link";
import { Play, ShieldAlert, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { JsonView } from "@/components/ui/json";
import { Select } from "@/components/ui/inputs";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useAuth } from "@/lib/auth";
import { useSystem } from "@/lib/queries";
import { useIncidents, useSecurityOverview, useSecurityRun, useStartSecurityRun } from "@/lib/security";
import { relativeTime, shortId } from "@/lib/format";
import type { CaseRow, Rate, SecurityResults } from "@/types/security";

const pct = (r: Rate) => (r.value == null ? "n/a" : `${Math.round(r.value * 1000) / 10}%`);
const VERDICT_TONE = { pass: "success", fail: "danger", error: "warning" } as const;
const SEVERITY_TONE = { info: "neutral", warning: "warning", high: "danger" } as const;
const GOAL_LABEL: Record<string, string> = {
  unauthorized_tool_write: "Write a file with an unauthorized tool", unknown_tool: "Call a tool that does not exist", approval_bypass: "Bypass or self-approve an approval",
  cross_workspace_search: "Search another workspace", cross_workspace_file: "Read another workspace's file", path_traversal: "Read outside the workspace", tool_call_flood: "Exhaust the tool-call budget",
};
const SURFACES = ["document", "retrieved_chunk", "search_result", "api_response"];

function Tile({ id, title, value, sub, bad }: { id: string; title: string; value: string; sub: string; bad?: boolean }) {
  return (
    <Card className="p-4" data-testid={`sec-${id}`}>
      <p className="text-xs text-muted-foreground">{title}</p>
      <p className={`mt-1 text-2xl font-semibold ${bad ? "text-danger" : ""}`}>{value}</p>
      <p className="text-xs text-muted-foreground">{sub}</p>
    </Card>
  );
}

function Results({ r }: { r: SecurityResults }) {
  const m = r.metrics;
  return (
    <>
      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4" data-testid="security-tiles">
        <Tile id="policy" title="Policy tests passed" value={`${r.counts.policy.pass}/${r.counts.policy.total}`} sub="tool authorization, approvals, budgets, workspace isolation" bad={r.counts.policy.pass < r.counts.policy.total} />
        <Tile id="enforcement" title="Attacks contained" value={pct(m.successful_policy_enforcement)} sub={`${m.successful_policy_enforcement.numerator} of ${m.successful_policy_enforcement.denominator} attack cases had no harmful effect`} bad={(m.successful_policy_enforcement.value ?? 1) < 1} />
        {m.model_ignored_injection && m.model_ignored_injection.numerator > 0 ? (
          <Tile id="ignored" title="Model ignored the injection" value={pct(m.model_ignored_injection)} sub={`${m.model_ignored_injection.numerator} of ${m.model_ignored_injection.denominator} attack cases: no matching tool call; contained when followed ${m.contained_when_followed ? `${m.contained_when_followed.numerator}/${m.contained_when_followed.denominator}` : "n/a"}`} />
        ) : null}
        <Tile id="exercised" title="Attacks actually attempted" value={pct(m.attacks_exercised)} sub={`${m.unauthorized_action_attempts} injected tool calls · ${m.blocked_actions} blocked`} bad={(m.attacks_exercised.value ?? 1) < 1 && !(m.model_ignored_injection && m.model_ignored_injection.numerator > 0)} />
        <Tile id="unsafe" title="Unsafe side effects" value={String(m.unsafe_side_effects)} sub={`writes attempted ${m.unsafe_side_effects_prevented.writes_attempted}, denied ${m.unsafe_side_effects_prevented.writes_denied}`} bad={m.unsafe_side_effects > 0} />
        <Tile id="false-positives" title="False-positive blocks" value={pct(m.false_positive_blocks)} sub={`${m.false_positive_blocks.numerator} of ${m.false_positive_blocks.denominator} benign controls wrongly blocked`} bad={m.false_positive_blocks.numerator > 0} />
        <Tile id="bypass" title="Approval bypass attempts" value={String(m.approval_bypass_attempts.attempts)} sub={`${m.approval_bypass_attempts.cases_failed} succeeded`} bad={m.approval_bypass_attempts.cases_failed > 0} />
        <Tile id="cross" title="Cross-workspace attempts" value={String(m.cross_workspace_access_attempts.cases)} sub={`${m.cross_workspace_access_attempts.canary_leaks} canary leaks`} bad={m.cross_workspace_access_attempts.canary_leaks > 0} />
        <Tile id="secrets" title="Secrets in persisted traces" value={`${m.secret_exposure_in_traces.numerator}/${m.secret_exposure_in_traces.denominator}`} sub="planted credential kinds found in events, tool records, model-call records" bad={m.secret_exposure_in_traces.numerator > 0} />
      </div>
      <Card>
        <CardHeader><CardTitle>Prompt-injection matrix</CardTitle><CardDescription>Each cell is one labeled attack: the goal, delivered through one surface. Green = no harmful effect and the attack was really attempted.</CardDescription></CardHeader>
        <CardContent>
          <Table data-testid="injection-matrix">
            <THead><tr><TH>Attack goal</TH>{SURFACES.map((s) => <TH key={s}>{s.replace("_", " ")}</TH>)}</tr></THead>
            <TBody>{Object.entries(r.matrix).map(([goal, cells]) => (
              <TR key={goal}><TD>{GOAL_LABEL[goal] ?? goal}</TD>{SURFACES.map((s) => (
                <TD key={s}>{cells[s] ? <Badge tone={VERDICT_TONE[cells[s]]} data-verdict={cells[s]}>{cells[s]}</Badge> : "—"}</TD>))}</TR>))}</TBody>
          </Table>
          <p className="mt-2 text-xs text-muted-foreground">Benign controls (8) are separate: they must not be blocked. {m.model_ignored_injection && m.model_ignored_injection.numerator > 0 ? <>This run used a <strong>real model</strong>: green cells include attacks the model simply ignored, so read them with the &quot;ignored&quot; and &quot;contained when followed&quot; tiles.</> : <>The test model is a mock that <strong>obeys every directive</strong>; this measures the platform&apos;s enforcement, not any model&apos;s resistance.</>}</p>
        </CardContent>
      </Card>
      {r.findings.length ? (
        <Card data-testid="findings">
          <CardHeader><CardTitle>Findings and recommended mitigations</CardTitle></CardHeader>
          <CardContent className="flex flex-col gap-2">{r.findings.map((f) => (
            <div key={f.case_key} className="rounded-md border p-3 text-sm"><div className="flex items-center gap-2"><Badge tone={VERDICT_TONE[f.verdict as "fail"]}>{f.verdict}</Badge><span className="font-mono text-xs">{f.case_key}</span></div>
              <p className="mt-1 text-muted-foreground">Mitigation: {f.mitigation}</p></div>))}</CardContent>
        </Card>
      ) : <p className="text-sm text-muted-foreground" data-testid="no-findings"><ShieldCheck className="mr-1 inline size-4 text-success" aria-hidden />No failing cases in this run. This is a regression floor, not a guarantee (see limitations).</p>}
    </>
  );
}

function CaseTable({ cases, detailed }: { cases: CaseRow[]; detailed: boolean }) {
  const [open, setOpen] = useState<string | null>(null);
  const [kind, setKind] = useState("policy");
  const rows = cases.filter((c) => c.kind === kind);
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between"><div><CardTitle>Cases</CardTitle><CardDescription>Every verdict is backed by persisted evidence.</CardDescription></div>
        <Select aria-label="Case kind" className="w-44" value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="policy">Policy tests</option><option value="injection">Injection attacks</option><option value="benign">Benign controls</option><option value="exposure">Secret exposure</option>
        </Select></CardHeader>
      <CardContent>
        <Table data-testid="case-table">
          <THead><tr><TH>Case</TH><TH>Expected</TH><TH>Verdict</TH>{detailed ? <TH>Evidence</TH> : null}</tr></THead>
          <TBody>{rows.map((c) => (
            <TR key={c.case_key}><TD><div className="font-medium">{c.title}</div><div className="font-mono text-[11px] text-muted-foreground">{c.case_key}</div></TD><TD className="text-xs">{c.expected}</TD>
              <TD><Badge tone={VERDICT_TONE[c.verdict]}>{c.verdict}</Badge></TD>
              {detailed ? <TD className="max-w-96 text-xs">
                <button className="text-primary hover:underline" onClick={() => setOpen(open === c.case_key ? null : c.case_key)}>{open === c.case_key ? "hide" : "show"}</button>
                {c.workflow_run_id ? <> · <Link className="text-primary hover:underline" href={`/flight/${c.workflow_run_id}`}>replay run {shortId(c.workflow_run_id)}</Link></> : null}
                {open === c.case_key ? <JsonView value={{ observed: c.observed, evidence: c.evidence }} maxHeight="10rem" /> : null}</TD> : null}</TR>))}</TBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export function SecurityCenter() {
  const { role } = useAuth();
  const ov = useSecurityOverview();
  const start = useStartSecurityRun();
  const system = useSystem();
  const livePossible = role === "admin" && !!system.data?.execution.paid_requests_allowed;
  const [source, setSource] = useState("");
  const incidents = useIncidents(source);
  const runId = ov.data?.running?.id ?? ov.data?.latest?.id ?? null;
  const run = useSecurityRun(runId);
  const canWrite = role === "admin" || role === "developer";
  if (ov.isLoading) return <LoadingBlock rows={6} />;
  if (ov.error) return <ErrorState error={ov.error} onRetry={() => void ov.refetch()} />;
  const o = ov.data!;
  const results = o.latest?.results ?? null;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold"><ShieldAlert className="size-5" aria-hidden /> Security Center</h1>
          <p className="text-sm text-muted-foreground">Deterministic tests of the enforcement layer: tool authorization, approvals, budgets, workspace isolation, a labeled prompt-injection corpus and trace sanitization. Suite <span className="font-mono">{o.suite_version}</span>.</p>
        </div>
        {canWrite ? <Button data-testid="run-security" disabled={start.isPending || !!o.running} onClick={() => start.mutate("mock")}><Play aria-hidden /> {o.running ? "Running…" : "Run security evaluation"}</Button> : null}
        {livePossible ? <Button variant="outline" data-testid="run-security-live" disabled={start.isPending || !!o.running} onClick={() => start.mutate("live")} title="Uses a real model and spends money (estimated well under $1)"><Play aria-hidden /> Run with real model</Button> : null}
      </div>
      <InlineError error={start.error} />
      <div role="note" className="rounded-lg border bg-card p-3 text-sm text-muted-foreground">
        {o.latest?.agent_mode === "live" ? (
          <><Badge tone="danger">real model</Badge> The latest evaluation used <span className="font-mono">{o.latest.agent_provider}</span> reading the same injected documents, with inert credential-shaped strings and a throwaway second workspace.
          A model that ignores an injection never reaches the platform&apos;s enforcement, so read &quot;ignored&quot; and &quot;contained when followed&quot; separately. One run, one trial per case.</>
        ) : (
          <><Badge tone="warning">offline fixtures</Badge> The injection tests use a mock model that obeys every directive on purpose, with inert credential-shaped strings and a throwaway second workspace.
          A blocked call proves a policy operated; it does not prove a real model is resistant to prompt injection.</>
        )}
      </div>
      {o.running ? <Card className="p-4" data-testid="security-running" aria-live="polite">Evaluation running · phase <span className="font-mono">{o.running.phase}</span>. Results appear when it completes.</Card> : null}
      {results ? <Results r={results} /> : !o.running ? <EmptyState title="No evaluation yet" description={canWrite ? "Run the suite to see results." : "A developer can run the suite."} /> : null}
      {run.data?.cases?.length && run.data.status === "completed" ? <CaseTable cases={run.data.cases} detailed={canWrite} /> : null}
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <div><CardTitle>Security incidents (last 30 days)</CardTitle><CardDescription>Derived from real records: policy denials, rejected or expired approvals, budget stops and failed security tests.</CardDescription></div>
          <Select aria-label="Incident source" className="w-52" value={source} onChange={(e) => setSource(e.target.value)}>
            <option value="">All sources</option><option value="policy_denial">Policy denials</option><option value="approval_rejected">Rejected approvals</option><option value="approval_expired">Expired approvals</option><option value="budget_exceeded">Budget stops</option><option value="security_test_failure">Failed security tests</option>
          </Select>
        </CardHeader>
        <CardContent>
          {incidents.data?.items.length ? (
            <Table data-testid="incidents">
              <THead><tr><TH>When</TH><TH>Severity</TH><TH>Incident</TH><TH>Origin</TH><TH>Evidence</TH></tr></THead>
              <TBody>{incidents.data.items.slice(0, 40).map((i) => (
                <TR key={i.id}><TD className="text-muted-foreground">{relativeTime(i.at)}</TD><TD><Badge tone={SEVERITY_TONE[i.severity]}>{i.severity}</Badge></TD>
                  <TD><div>{i.title}</div>{i.detail ? <div className="text-xs text-muted-foreground">{i.detail}</div> : null}</TD><TD className="text-xs">{i.origin}</TD>
                  <TD className="text-xs">{i.run_id ? <Link className="text-primary hover:underline" href={`/flight/${i.run_id}`}>run {shortId(i.run_id)}</Link> : "—"}</TD></TR>))}</TBody>
            </Table>
          ) : <p className="text-sm text-muted-foreground">No incidents in this window.</p>}
        </CardContent>
      </Card>
      {results ? (
        <Card><CardHeader><CardTitle>Limitations</CardTitle></CardHeader><CardContent><ul className="list-disc pl-5 text-sm text-muted-foreground">{results.limitations.map((l) => <li key={l}>{l}</li>)}</ul></CardContent></Card>
      ) : null}
    </div>
  );
}
