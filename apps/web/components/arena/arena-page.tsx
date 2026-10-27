"use client";

import Link from "next/link";
import { Lock, Play, Swords, Trophy } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyState, ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Input, Select } from "@/components/ui/inputs";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/lib/auth";
import { useArena, useArenaCases, useArenaRuns, useContenders, useCorpus, useCreateArena, useFinalTest } from "@/lib/arena";
import { shortId } from "@/lib/format";
import type { ArenaDetail, Family, LeaderRow, Leaderboard, Split } from "@/types/arena";

const FAMILIES: { id: Family; label: string }[] = [
  { id: "extraction", label: "Extraction" },
  { id: "calculation", label: "Calculation" },
  { id: "grounded_qa", label: "Grounded Q&A" },
];
const STAGE_LABEL: Record<string, string> = {
  selection_running: "Scoring development + validation…",
  selection_complete: "Selection complete · test split locked",
  final_test_running: "Scoring the test split…",
  completed: "Complete",
  failed: "Failed",
};
const pct = (v: number | null | undefined) => (v == null ? "n/a" : `${(v * 100).toFixed(1)}%`);
const ci = (v: [number, number] | null | undefined) => (v ? `${(v[0] * 100).toFixed(0)}–${(v[1] * 100).toFixed(0)}%` : "");

/** A score bar with its 95% interval as a whisker. The numbers are always printed beside it. */
function ScoreBar({ score, interval }: { score: number | null; interval: [number, number] | null }) {
  if (score == null) return <span className="text-muted-foreground">n/a</span>;
  return (
    <div className="flex items-center gap-2">
      <div className="relative h-3 w-40 rounded-sm bg-muted" aria-hidden>
        <div className="absolute inset-y-0 left-0 rounded-sm bg-primary" style={{ width: `${score * 100}%` }} />
        {interval ? <div className="absolute top-1/2 h-px -translate-y-1/2 bg-foreground" style={{ left: `${interval[0] * 100}%`, width: `${(interval[1] - interval[0]) * 100}%` }} /> : null}
      </div>
      <span className="font-mono text-xs">{pct(score)}</span>
      <span className="text-[11px] text-muted-foreground">{ci(interval)}</span>
    </div>
  );
}

function Board({ split, board, onPick, picked }: { split: Split; board: Leaderboard; onPick: (r: LeaderRow) => void; picked: string | null }) {
  return (
    <div className="flex flex-col gap-3">
      <Table data-testid={`leaderboard-${split}`}>
        <THead><tr><TH>Rank</TH><TH>Agent</TH><TH>Overall (95% interval)</TH>{FAMILIES.map((f) => <TH key={f.id}>{f.label}</TH>)}<TH>Cost</TH><TH>Latency p50</TH><TH>Top failure</TH></tr></THead>
        <TBody>{board.rows.map((r) => (
          <TR key={r.agent_version_id} className={picked === r.agent_version_id ? "bg-muted/50" : ""}>
            <TD data-testid="rank">{r.rank}</TD>
            <TD><button className="font-medium text-primary hover:underline" onClick={() => onPick(r)}>{r.agent}</button><div className="font-mono text-[11px] text-muted-foreground">v{r.version} · {r.config_hash}</div></TD>
            <TD><ScoreBar score={r.overall.score} interval={r.overall.ci95} /></TD>
            {FAMILIES.map((f) => <TD key={f.id} className="text-xs">{pct(r.families[f.id]?.rate)} <span className="text-muted-foreground">({r.families[f.id]?.passed}/{r.families[f.id]?.n})</span></TD>)}
            <TD className="text-xs">{r.cost_usd == null ? "n/a" : `$${r.cost_usd.toFixed(4)}`}<div className="text-[11px] text-muted-foreground">simulated</div></TD>
            <TD className="text-xs">{r.latency_ms.p50 == null ? "n/a" : `${Math.round(r.latency_ms.p50)} ms`}</TD>
            <TD className="text-xs">{Object.entries(r.failure_breakdown)[0]?.join(" × ") ?? "—"}</TD>
          </TR>))}</TBody>
      </Table>
      <p className="text-xs text-muted-foreground">
        Rank is a competition rank with statistical ties: contenders share a rank when the paired bootstrap interval of their difference includes 0. Overall = unweighted mean of the three family pass rates (a case passes only if every deterministic evaluator passes). {board.method.intervals}.
      </p>
      {board.adjacent_comparisons.some((c) => !c.separated) ? <p className="text-xs text-muted-foreground" data-testid={`ties-${split}`}>Not separated: {board.adjacent_comparisons.filter((c) => !c.separated).map((c) => `${c.higher} vs ${c.lower}`).join("; ")}.</p> : null}
    </div>
  );
}

function Failures({ arena, split, versionId, role }: { arena: ArenaDetail; split: Split; versionId: string | null; role: string | null }) {
  const cases = useArenaCases(arena.id, split, versionId, !!versionId);
  if (!versionId) return <p className="text-sm text-muted-foreground">Select an agent in the leaderboard to see its failed cases.</p>;
  if (cases.isLoading) return <LoadingBlock rows={3} />;
  const items = cases.data?.items ?? [];
  const privileged = role === "admin" || role === "developer";
  return (
    <div className="flex flex-col gap-2" data-testid="arena-failures">
      {cases.data?.expected_hidden ? <p className="text-xs text-muted-foreground">Expected answers for {split} cases are hidden by design; only the failure category is shown.</p> : null}
      {items.length === 0 ? <p className="text-sm text-muted-foreground">No failed cases for this agent on {split}.</p> : (
        <Table>
          <THead><tr><TH>Family</TH><TH>Case</TH><TH>Kind</TH><TH>Failure category</TH><TH>Run</TH></tr></THead>
          <TBody>{items.map((c) => (
            <TR key={`${c.case_key}`}><TD className="text-xs">{c.family}</TD><TD className="font-mono text-xs">{c.case_key}</TD><TD className="text-xs">{c.kind ?? "—"}</TD>
              <TD className="text-xs">{c.category ?? c.error_type ?? "—"}</TD>
              <TD className="text-xs">{c.workflow_run_id && privileged ? <Link className="text-primary hover:underline" href={`/flight/${c.workflow_run_id}`}>replay {shortId(c.workflow_run_id)}</Link> : "—"}</TD></TR>))}</TBody>
        </Table>
      )}
    </div>
  );
}

function RunView({ id }: { id: string }) {
  const { role } = useAuth();
  const arena = useArena(id);
  const final = useFinalTest(id);
  const [picked, setPicked] = useState<{ split: Split; versionId: string } | null>(null);
  const [tab, setTab] = useState<Split>("validation");
  if (arena.isLoading) return <LoadingBlock rows={4} />;
  if (arena.error) return <ErrorState error={arena.error} onRetry={() => void arena.refetch()} />;
  const a = arena.data!;
  const canWrite = role === "admin" || role === "developer";
  const running = a.stage === "selection_running" || a.stage === "final_test_running";
  const progress = Object.entries(a.progress).map(([s, c]) => `${s}: ${c.completed ?? 0}/${Object.values(c).reduce((x, y) => x + y, 0)}`).join(" · ");
  return (
    <div className="flex flex-col gap-3" data-testid="arena-run">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div><h2 className="text-lg font-semibold">{a.name}</h2>
          <p className="text-xs text-muted-foreground"><span data-testid="arena-stage" className="font-medium text-foreground">{STAGE_LABEL[a.stage]}</span> · {a.config.contenders.length} contenders · evaluations complete {progress}</p></div>
        {canWrite && a.stage === "selection_complete" ? <Button data-testid="final-test" disabled={final.isPending} onClick={() => final.mutate()}><Lock aria-hidden /> Score the test split (once)</Button> : null}
      </div>
      <InlineError error={final.error} />
      {running ? <Card className="p-3 text-sm" aria-live="polite">Evaluations are running on the workers. Rankings appear when a split is complete.</Card> : null}
      {a.stage === "selection_complete" ? (
        <p className="text-xs text-muted-foreground" data-testid="test-locked"><Lock className="mr-1 inline size-3" aria-hidden />The test split has not been scored. Choose using development and validation; scoring test is a single, irreversible step for this arena run.</p>
      ) : null}
      {Object.keys(a.leaderboards).length ? (
        <Tabs value={tab} onValueChange={(v) => setTab(v as Split)}>
          <TabsList>
            {(["development", "validation", "test"] as Split[]).map((s) => <TabsTrigger key={s} value={s} disabled={!a.leaderboards[s]} data-testid={`tab-${s}`}>{s}{!a.leaderboards[s] ? " 🔒" : ""}</TabsTrigger>)}
          </TabsList>
          {(["development", "validation", "test"] as Split[]).map((s) => a.leaderboards[s] ? (
            <TabsContent key={s} value={s}>
              <Board split={s} board={a.leaderboards[s]!} picked={picked?.split === s ? picked.versionId : null} onPick={(r) => setPicked({ split: s, versionId: r.agent_version_id })} />
              {picked?.split === s ? <div className="mt-3"><Failures arena={a} split={s} versionId={picked.versionId} role={role} /></div> : null}
            </TabsContent>) : null)}
        </Tabs>
      ) : null}
      {a.selection_vs_test ? (
        <Card data-testid="selection-vs-test">
          <CardHeader><CardTitle>Validation vs test</CardTitle><CardDescription>A large drop from the split used for selection to the held-out split would suggest the choice was fitted to validation (gap above 10 points is flagged).</CardDescription></CardHeader>
          <CardContent>
            <Table>
              <THead><tr><TH>Agent</TH><TH>Validation</TH><TH>Test</TH><TH>Gap</TH><TH>Rank val → test</TH></tr></THead>
              <TBody>{a.selection_vs_test.map((c) => (
                <TR key={c.agent_version_id}><TD>{c.agent}</TD><TD>{pct(c.validation_score)}</TD><TD>{pct(c.test_score)}</TD>
                  <TD>{c.gap == null ? "n/a" : `${(c.gap * 100).toFixed(1)} pts`} {c.gap_flag ? <Badge tone="warning">large gap</Badge> : null}</TD><TD>{c.validation_rank} → {c.test_rank}</TD></TR>))}</TBody>
            </Table>
          </CardContent>
        </Card>
      ) : null}
    </div>
  );
}

export function ArenaPage() {
  const { role } = useAuth();
  const corpus = useCorpus();
  const contenders = useContenders();
  const runs = useArenaRuns();
  const create = useCreateArena();
  const [selected, setSelected] = useState<string[]>([]);
  const [name, setName] = useState("Arena run");
  const [runId, setRunId] = useState<string | null>(null);
  const canWrite = role === "admin" || role === "developer";
  if (corpus.isLoading || contenders.isLoading) return <LoadingBlock rows={6} />;
  if (corpus.error) return <ErrorState error={corpus.error} onRetry={() => void corpus.refetch()} />;
  const c = corpus.data!;
  const eligible = (contenders.data ?? []).filter((x) => x.eligible && x.arena_contender);
  const activeRun = runId ?? runs.data?.[0]?.id ?? null;
  const byFamily = FAMILIES.map((f) => ({ ...f, rows: c.datasets.filter((d) => d.family === f.id) }));
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="flex items-center gap-2 text-xl font-semibold"><Swords className="size-5" aria-hidden /> Agent Arena</h1>
        <p className="text-sm text-muted-foreground">Compare agent configurations on {c.total_cases} labeled cases in three task families, with development / validation / test splits, pinned dataset hashes and rankings that account for statistical ties.</p>
      </div>
      <div role="note" className="rounded-lg border bg-card p-3 text-sm text-muted-foreground">
        <Badge tone="warning">offline · scripted contenders</Badge> The contenders are deterministic scripted policies that differ only in prompt, tools and step limit. The Arena measures the evaluation machinery and the effect of configuration; <strong>it says nothing about how any real model would score</strong>. No paid model is called, and paid providers are refused.
      </div>
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <div><CardTitle>Corpus</CardTitle><CardDescription>Version {c.version}, split salt {c.split_salt}. Each dataset&apos;s hash is checked against the committed manifest.</CardDescription></div>
          <Badge tone={c.all_verified ? "success" : "danger"} data-testid="corpus-state">{c.all_verified ? "all datasets verified" : "hash mismatch or missing"}</Badge>
        </CardHeader>
        <CardContent>
          <Table data-testid="arena-corpus">
            <THead><tr><TH>Family</TH><TH>development</TH><TH>validation</TH><TH>test</TH><TH>Case kinds</TH></tr></THead>
            <TBody>{byFamily.map((f) => (
              <TR key={f.id}><TD className="font-medium">{f.label}</TD>
                {(["development", "validation", "test"] as Split[]).map((s) => { const d = f.rows.find((x) => x.split === s); return <TD key={s} className="text-xs">{d ? <>{d.cases} <span className="font-mono text-[11px] text-muted-foreground">{d.manifest_hash.slice(0, 8)}</span></> : "—"}</TD>; })}
                <TD className="text-xs text-muted-foreground">{Array.from(new Set(f.rows.flatMap((r) => Object.keys(r.kinds)))).join(", ")}</TD></TR>))}</TBody>
          </Table>
          <p className="mt-2 text-xs text-muted-foreground" data-testid="leakage-note">
            Identical cases across splits: {c.leakage.identical_case_across_splits}. Identical fact sets across splits: {Object.values(c.leakage.identical_fact_set_across_splits).reduce((x, y) => x + y, 0)}. The documents contain only {c.leakage.distinct_facts_in_documents} distinct facts, so individual facts recur across splits in different combinations ({Object.entries(c.leakage.facts_shared_between_splits).map(([k, v]) => `${k}: ${v}`).join(", ")}). Splits stop tuning on the cases used for selection; they do not make facts novel.
          </p>
        </CardContent>
      </Card>
      {canWrite ? (
        <Card>
          <CardHeader><CardTitle>New arena run</CardTitle><CardDescription>Scores each contender on development and validation. The test split is scored later, once.</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-3">
            <div className="grid gap-2 md:grid-cols-2" data-testid="contender-list">
              {eligible.map((x) => (
                <label key={x.agent_id} className="flex cursor-pointer items-start gap-2 rounded-md border p-2 text-sm">
                  <input type="checkbox" className="mt-1" checked={selected.includes(x.agent_version_id!)} onChange={(e) => setSelected((s) => e.target.checked ? [...s, x.agent_version_id!] : s.filter((v) => v !== x.agent_version_id))} />
                  <span><span className="font-medium">{x.name}</span><span className="block text-xs text-muted-foreground">{x.description.replace("Agent Arena contender. ", "")} · tools: {x.tools.join(", ") || "none"} · max steps {x.max_steps}</span></span>
                </label>))}
              {eligible.length === 0 ? <EmptyState title="No contenders" description="Seed the demo workspace to get sample contenders." /> : null}
            </div>
            <div className="flex items-end gap-2">
              <div className="w-full sm:w-64"><Input aria-label="Arena run name" value={name} onChange={(e) => setName(e.target.value)} /></div>
              <Button data-testid="start-arena" disabled={create.isPending || selected.length < 2 || !name.trim()} onClick={() => create.mutate({ name: name.trim(), agent_version_ids: selected }, { onSuccess: (r) => setRunId(r.id) })}><Play aria-hidden /> Run selection ({selected.length} chosen)</Button>
            </div>
            <InlineError error={create.error} />
          </CardContent>
        </Card>
      ) : null}
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <div><CardTitle className="flex items-center gap-2"><Trophy className="size-4" aria-hidden /> Leaderboard</CardTitle></div>
          {runs.data?.length ? <Select aria-label="Arena run" className="w-full max-w-full sm:w-64" value={activeRun ?? ""} onChange={(e) => setRunId(e.target.value)}>{runs.data.map((r) => <option key={r.id} value={r.id}>{r.name} · {new Date(r.created_at).toLocaleString()}</option>)}</Select> : null}
        </CardHeader>
        <CardContent>{activeRun ? <RunView id={activeRun} /> : <EmptyState title="No arena runs yet" description={canWrite ? "Pick at least two contenders above." : "A developer can start an arena run."} />}</CardContent>
      </Card>
    </div>
  );
}
