"use client";

import { Brain, Play, Search } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Input, Select, Textarea } from "@/components/ui/inputs";
import { JsonView } from "@/components/ui/json";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/lib/auth";
import { useAudit, useExperiment, useExperiments, useHistory, useMemoryMutations, useRecords } from "@/lib/memory";
import type { MemoryRecord, StrategyResult } from "@/types/memory";

const pct = (v: number | null | undefined) => (v == null ? "n/a" : `${(v * 100).toFixed(0)}%`);
const STATE_TONE = { live: "success", deleted: "neutral", expired: "warning" } as const;
const STRATEGY_LABEL: Record<string, string> = {
  none: "No memory", short_term: "Short-term", persistent_full: "Persistent (all records)", summarized_stale: "Summarized (not refreshed)", summarized_refreshed: "Summarized (refreshed)", retrieval_top3: "Retrieval (top 3)",
};

function RecordsTab({ canWrite }: { canWrite: boolean }) {
  const [ns, setNs] = useState("");
  const [inactive, setInactive] = useState(false);
  const records = useRecords(ns, inactive);
  const m = useMemoryMutations();
  const [open, setOpen] = useState<MemoryRecord | null>(null);
  const history = useHistory(open?.id ?? null);
  const [form, setForm] = useState({ kind: "kv", key: "", content: "", namespace: "default", tags: "" });
  const [edit, setEdit] = useState("");
  return (
    <div className="grid gap-4 lg:grid-cols-[2fr_1fr]">
      <Card>
        <CardHeader className="flex-row items-center justify-between gap-2">
          <div><CardTitle>Records</CardTitle><CardDescription>Workspace-scoped, versioned. Content that looks like a credential is refused.</CardDescription></div>
          <div className="flex items-center gap-2">
            <Input aria-label="Namespace filter" placeholder="namespace" className="w-36" value={ns} onChange={(e) => setNs(e.target.value)} />
            <label className="flex items-center gap-1 text-xs"><input type="checkbox" checked={inactive} onChange={(e) => setInactive(e.target.checked)} /> include deleted/expired</label>
          </div>
        </CardHeader>
        <CardContent>
          {records.isLoading ? <LoadingBlock rows={3} /> : records.error ? <ErrorState error={records.error} onRetry={() => void records.refetch()} /> : !records.data?.length ? <EmptyState title="No memory records" description={canWrite ? "Create one on the right." : "A developer can create records."} /> : (
            <Table data-testid="memory-records">
              <THead><tr><TH>Namespace / key</TH><TH>Content</TH><TH>v</TH><TH>State</TH></tr></THead>
              <TBody>{records.data.map((r) => (
                <TR key={r.id} className={open?.id === r.id ? "bg-muted/50" : ""}>
                  <TD><button className="text-left text-primary hover:underline" onClick={() => { setOpen(r); setEdit(r.content); }}><span className="font-mono text-xs">{r.namespace}</span><br />{r.key ?? <em>unkeyed {r.kind}</em>}</button></TD>
                  <TD className="max-w-md truncate text-xs">{r.content}</TD><TD>{r.version}</TD><TD><Badge tone={STATE_TONE[r.state]}>{r.state}</Badge></TD>
                </TR>))}</TBody>
            </Table>)}
        </CardContent>
      </Card>
      <div className="flex flex-col gap-4">
        {canWrite ? (
          <Card>
            <CardHeader><CardTitle>New record</CardTitle></CardHeader>
            <CardContent className="flex flex-col gap-2">
              <Select aria-label="Kind" value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}><option value="kv">Key-value</option><option value="note">Note</option><option value="task_summary">Task summary</option></Select>
              <Input aria-label="Key" placeholder="key (required for key-value)" value={form.key} onChange={(e) => setForm({ ...form, key: e.target.value })} />
              <Input aria-label="Namespace" value={form.namespace} onChange={(e) => setForm({ ...form, namespace: e.target.value })} />
              <Textarea aria-label="Content" placeholder="What should be remembered?" value={form.content} onChange={(e) => setForm({ ...form, content: e.target.value })} />
              <Input aria-label="Tags" placeholder="tags, comma separated" value={form.tags} onChange={(e) => setForm({ ...form, tags: e.target.value })} />
              <Button data-testid="create-record" disabled={m.create.isPending || !form.content.trim()} onClick={() => m.create.mutate({ kind: form.kind, key: form.key || undefined, content: form.content, namespace: form.namespace, tags: form.tags.split(",").map((t) => t.trim()).filter(Boolean) }, { onSuccess: () => setForm({ ...form, key: "", content: "" }) })}>Store memory</Button>
              <InlineError error={m.create.error} />
            </CardContent>
          </Card>) : null}
        {open ? (
          <Card data-testid="record-detail">
            <CardHeader><CardTitle className="text-base">{open.key ?? open.kind} · v{open.version}</CardTitle><CardDescription>Provenance and history. Content is untrusted context when read back by an agent.</CardDescription></CardHeader>
            <CardContent className="flex flex-col gap-2">
              <JsonView value={open.provenance} maxHeight="8rem" />
              {canWrite && open.state === "live" ? (
                <>
                  <Textarea aria-label="Edit content" value={edit} onChange={(e) => setEdit(e.target.value)} />
                  <div className="flex gap-2">
                    <Button size="sm" data-testid="update-record" disabled={m.update.isPending || edit === open.content} onClick={() => m.update.mutate({ id: open.id, expected_version: open.version, content: edit }, { onSuccess: (r) => setOpen(r as unknown as MemoryRecord) })}>Save new version</Button>
                    <Button size="sm" variant="outline" disabled={m.remove.isPending} onClick={() => m.remove.mutate(open.id, { onSuccess: () => setOpen(null) })}>Delete</Button>
                  </div>
                  <InlineError error={m.update.error ?? m.remove.error} />
                </>) : null}
              <Table data-testid="record-history">
                <THead><tr><TH>v</TH><TH>change</TH><TH>content</TH><TH>by</TH></tr></THead>
                <TBody>{(history.data ?? []).map((v) => <TR key={`${v.version}-${v.change_type}`}><TD>{v.version}</TD><TD>{v.change_type}</TD><TD className="max-w-48 truncate text-xs">{v.content}</TD><TD className="text-xs">{v.changed_by}</TD></TR>)}</TBody>
              </Table>
            </CardContent>
          </Card>) : null}
      </div>
    </div>
  );
}

function SearchTab() {
  const m = useMemoryMutations();
  const [q, setQ] = useState("");
  const [ns, setNs] = useState("");
  return (
    <Card>
      <CardHeader><CardTitle>Retrieval tester</CardTitle><CardDescription>Keyword retrieval with metadata filters. Every result carries a relevance score and provenance, and is marked untrusted.</CardDescription></CardHeader>
      <CardContent className="flex flex-col gap-3">
        <div className="flex gap-2">
          <Input aria-label="Query" placeholder="e.g. Atlas owner" value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => e.key === "Enter" && q.trim() && m.search.mutate({ query: q, namespace: ns })} />
          <Input aria-label="Search namespace" placeholder="namespace (optional)" className="w-48" value={ns} onChange={(e) => setNs(e.target.value)} />
          <Button data-testid="run-search" disabled={m.search.isPending || !q.trim()} onClick={() => m.search.mutate({ query: q, namespace: ns })}><Search aria-hidden /> Search</Button>
        </div>
        <InlineError error={m.search.error} />
        {m.search.data ? (
          <div data-testid="search-results" className="flex flex-col gap-2">
            <p className="text-xs text-muted-foreground">{m.search.data.method}</p>
            {m.search.data.items.length === 0 ? <p className="text-sm text-muted-foreground">No live memory matched.</p> : m.search.data.items.map((h) => (
              <div key={h.id} className="rounded-md border p-3 text-sm">
                <div className="flex items-center gap-2"><Badge tone="warning">untrusted</Badge><span className="font-mono text-xs">{h.namespace}/{h.key ?? h.kind}</span><span className="ml-auto text-xs text-muted-foreground">score {h.score.toFixed(3)} · v{h.version}</span></div>
                <p className="mt-1">{h.content}</p>
              </div>))}
          </div>) : null}
      </CardContent>
    </Card>
  );
}

function Experiments({ canWrite }: { canWrite: boolean }) {
  const list = useExperiments();
  const m = useMemoryMutations();
  const [picked, setPicked] = useState<string | null>(null);
  const id = picked ?? list.data?.items[0]?.id ?? null;
  const exp = useExperiment(id);
  const running = list.data?.items.some((e) => e.status === "running");
  const r = exp.data?.results;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="max-w-3xl text-sm text-muted-foreground">Six ways of giving the same scripted reader memory, on 38 questions with known answers (updated, expired, in-conversation, missing and look-alike facts). Memory does not always help, and this measures where it does not.</p>
        <div className="flex items-center gap-2">
          {list.data?.items.length ? <Select aria-label="Experiment" className="w-56" value={id ?? ""} onChange={(e) => setPicked(e.target.value)}>{list.data.items.map((e) => <option key={e.id} value={e.id}>{new Date(e.created_at).toLocaleString()} · {e.status}</option>)}</Select> : null}
          {canWrite ? <Button data-testid="run-experiment" disabled={m.startExperiment.isPending || !!running} onClick={() => m.startExperiment.mutate(undefined, { onSuccess: (e) => setPicked(e.id) })}><Play aria-hidden /> {running ? "Running…" : "Run experiment"}</Button> : null}
        </div>
      </div>
      <InlineError error={m.startExperiment.error} />
      {exp.data?.status === "running" ? <Card className="p-4 text-sm" aria-live="polite" data-testid="experiment-running">Evaluating six strategies on the workers…</Card> : null}
      {exp.data?.status === "failed" ? <ErrorState error={new Error(exp.data.error ?? "failed")} /> : null}
      {!id ? <EmptyState title="No experiments yet" description={canWrite ? "Run the experiment to compare strategies." : "A developer can run experiments."} /> : null}
      {r ? (
        <>
          <Card data-testid="strategy-table-card">
            <CardHeader><CardTitle>Results by strategy</CardTitle><CardDescription>{r.tasks} questions over {r.facts_stored} stored facts. Every figure comes from graded executions; denominators are shown.</CardDescription></CardHeader>
            <CardContent>
              <Table data-testid="strategy-table">
                <THead><tr><TH>Strategy</TH><TH>Accuracy</TH><TH>Stale answers</TH><TH>Wrong-entity reuse</TH><TH>Retrieval precision</TH><TH>Answer recall</TH><TH>Context (chars / ~tokens)</TH><TH>Latency p50</TH></tr></THead>
                <TBody>{Object.entries(r.strategies).map(([k, s]: [string, StrategyResult]) => (
                  <TR key={k} data-strategy={k}>
                    <TD><div className="font-medium">{STRATEGY_LABEL[k] ?? k}</div><div className="max-w-56 text-[11px] text-muted-foreground">{exp.data?.definitions?.[k]}</div></TD>
                    <TD data-testid={`acc-${k}`}>{pct(s.accuracy.value)} <span className="text-xs text-muted-foreground">({s.accuracy.numerator}/{s.accuracy.denominator})</span></TD>
                    <TD data-testid={`stale-${k}`}>{s.stale_memory_errors.count}<span className="text-xs text-muted-foreground"> / {s.stale_memory_errors.denominator}</span></TD>
                    <TD data-testid={`reuse-${k}`}>{s.incorrect_memory_reuse.count}<span className="text-xs text-muted-foreground"> / {s.incorrect_memory_reuse.denominator}</span></TD>
                    <TD className="text-xs">{s.retrieval_precision.value == null ? "n/a" : `${pct(s.retrieval_precision.value)} (${s.retrieval_precision.numerator}/${s.retrieval_precision.denominator})`}</TD>
                    <TD className="text-xs">{pct(s.answer_recall.value)}</TD>
                    <TD className="text-xs">{Math.round(s.context.mean_chars)} / ~{Math.round(s.context.mean_tokens_est)}</TD>
                    <TD className="text-xs">{s.latency_ms.p50 == null ? "n/a" : `${Math.round(s.latency_ms.p50)} ms`}</TD>
                  </TR>))}</TBody>
              </Table>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Accuracy by question type</CardTitle></CardHeader>
            <CardContent>
              <Table>
                <THead><tr><TH>Strategy</TH>{["current", "updated", "expired", "inline", "missing", "near_match"].map((k) => <TH key={k}>{k.replace("_", " ")}</TH>)}</tr></THead>
                <TBody>{Object.entries(r.strategies).map(([k, s]) => <TR key={k}><TD>{STRATEGY_LABEL[k] ?? k}</TD>{["current", "updated", "expired", "inline", "missing", "near_match"].map((kind) => <TD key={kind} className="text-xs">{s.by_kind[kind] ? `${s.by_kind[kind].correct}/${s.by_kind[kind].n}` : "—"}</TD>)}</TR>)}</TBody>
              </Table>
            </CardContent>
          </Card>
          <Card>
            <CardHeader><CardTitle>Paired comparisons</CardTitle><CardDescription>Same questions, different strategy. Supported only if the bootstrap interval excludes 0 (at least 10 matched questions).</CardDescription></CardHeader>
            <CardContent>
              <Table data-testid="comparisons">
                <THead><tr><TH>Candidate</TH><TH>vs baseline</TH><TH>Difference</TH><TH>95% interval</TH><TH>McNemar p</TH><TH>Supported?</TH></tr></THead>
                <TBody>{r.comparisons.map((c) => <TR key={`${c.candidate}-${c.baseline}`}><TD>{STRATEGY_LABEL[c.candidate]}</TD><TD>{STRATEGY_LABEL[c.baseline]}</TD><TD>{c.diff_mean == null ? "n/a" : `${c.diff_mean >= 0 ? "+" : ""}${(c.diff_mean * 100).toFixed(1)} pts`}</TD><TD className="text-xs">{c.ci95 ? `${(c.ci95[0] * 100).toFixed(0)} to ${(c.ci95[1] * 100).toFixed(0)}` : "n/a"}</TD><TD className="text-xs">{c.mcnemar_p == null ? "n/a" : c.mcnemar_p.toFixed(4)}</TD><TD><Badge tone={c.significant ? "success" : "neutral"}>{c.significant ? "yes" : "not shown"}</Badge></TD></TR>)}</TBody>
              </Table>
              <ul className="mt-3 list-disc pl-5 text-xs text-muted-foreground">{r.caveats.map((c) => <li key={c}>{c}</li>)}</ul>
            </CardContent>
          </Card>
        </>) : null}
    </div>
  );
}

function AuditTab() {
  const audit = useAudit(true);
  if (audit.isLoading) return <LoadingBlock rows={4} />;
  return (
    <Card>
      <CardHeader><CardTitle>Memory audit log</CardTitle><CardDescription>Append-only: creates, updates, deletes, expiries, agent searches and refused writes.</CardDescription></CardHeader>
      <CardContent>
        <Table data-testid="memory-audit">
          <THead><tr><TH>When</TH><TH>Action</TH><TH>Actor</TH><TH>Details</TH></tr></THead>
          <TBody>{(audit.data ?? []).map((a) => <TR key={a.id}><TD className="text-xs">{new Date(a.at).toLocaleString()}</TD><TD><Badge tone={a.action === "denied" ? "danger" : "neutral"}>{a.action}</Badge></TD><TD className="text-xs">{a.actor_type}</TD><TD className="max-w-md truncate font-mono text-[11px]">{JSON.stringify(a.details)}</TD></TR>)}</TBody>
        </Table>
      </CardContent>
    </Card>
  );
}

export function MemoryLab() {
  const { role } = useAuth();
  const canWrite = role === "admin" || role === "developer";
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold"><Brain className="size-5" aria-hidden /> Memory Lab</h1>
        <p className="text-sm text-muted-foreground">Durable, versioned, audited agent memory in PostgreSQL, keyword retrieval with provenance, and experiments comparing memory strategies.</p>
      </div>
      <div role="note" className="rounded-lg border bg-card p-3 text-sm text-muted-foreground">
        <Badge tone="warning">offline · scripted reader</Badge> Retrieved memory is untrusted context. Experiments use a scripted reader with fixed matching rules, so they show how each strategy&apos;s context affects it; they do not show how a real model uses memory. No embeddings are used.
      </div>
      <Tabs defaultValue="records">
        <TabsList><TabsTrigger value="records">Records</TabsTrigger><TabsTrigger value="search">Retrieval</TabsTrigger><TabsTrigger value="experiments">Experiments</TabsTrigger>{canWrite ? <TabsTrigger value="audit">Audit log</TabsTrigger> : null}</TabsList>
        <TabsContent value="records"><RecordsTab canWrite={canWrite} /></TabsContent>
        <TabsContent value="search"><SearchTab /></TabsContent>
        <TabsContent value="experiments"><Experiments canWrite={canWrite} /></TabsContent>
        {canWrite ? <TabsContent value="audit"><AuditTab /></TabsContent> : null}
      </Tabs>
    </div>
  );
}
