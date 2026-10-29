"use client";

import { useRouter } from "next/navigation";
import { ChevronLeft, ChevronRight, Download, Pause, Play, Rewind, SkipBack, SkipForward, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { InlineError } from "@/components/ui/feedback";
import { Select } from "@/components/ui/inputs";
import { JsonView, KV } from "@/components/ui/json";
import { StatusBadge } from "@/components/ui/status";
import { type DagNodeSpec, type DagNodeState, DagGraph } from "@/components/workflows/dag-graph";
import { useAuth } from "@/lib/auth";
import { exportFlight, useModelCall, useReexecute } from "@/lib/flight";
import { formatCost, formatDuration, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";
import type { FlightFrame, FlightRecording } from "@/types/flight";

const SEVERITY_COLOR: Record<string, string> = { error: "bg-danger", warning: "bg-warning", info: "bg-muted-foreground/40" };
const SPEEDS = [0.5, 1, 2, 4, 8];

export function FlightPlayer({ rec }: { rec: FlightRecording }) {
  const router = useRouter();
  const { role } = useAuth();
  const frames = rec.frames;
  const [idx, setIdx] = useState(frames.length); // frames applied; start at the end state
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(2);
  const [selected, setSelected] = useState<string | null>(null);
  const reexec = useReexecute(rec.run.id);
  const canWrite = role === "admin" || role === "developer";

  // Snapshot i = step statuses after the first i frames. Playback only ever applies recorded deltas.
  const snapshots = useMemo(() => {
    const out: Record<string, string>[] = [{ ...rec.initial_status }];
    let cur = { ...rec.initial_status };
    for (const f of frames) {
      cur = { ...cur, ...f.status_delta };
      out.push(cur);
    }
    return out;
  }, [rec, frames]);
  const status = snapshots[idx];
  const frame: FlightFrame | null = idx > 0 ? frames[idx - 1] : null;
  const nowMs = frame?.elapsed_ms ?? 0;
  const totalMs = frames.at(-1)?.elapsed_ms ?? 1;

  const active = playing && idx < frames.length; // playback stops by itself at the last frame
  const togglePlay = () => {
    if (active) {
      setPlaying(false);
      return;
    }
    if (idx >= frames.length) setIdx(0); // play again from the start
    setPlaying(true);
  };

  const step = useCallback((d: number) => setIdx((i) => Math.min(frames.length, Math.max(0, i + d))), [frames.length]);

  useEffect(() => {
    if (!playing || idx >= frames.length) return;
    const next = frames[idx];
    const gap = Math.max(0, (next.elapsed_ms ?? 0) - (frame?.elapsed_ms ?? 0));
    const t = setTimeout(() => setIdx((i) => i + 1), Math.min(700, Math.max(40, gap / speed)));
    return () => clearTimeout(t);
  }, [playing, idx, speed, frames, frame]);

  const onKey = (e: React.KeyboardEvent) => {
    if (e.target instanceof HTMLInputElement || e.target instanceof HTMLSelectElement) return;
    if (e.key === "ArrowRight") step(1);
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === " ") {
      e.preventDefault();
      togglePlay();
    } else if (e.key === "Home") setIdx(0);
    else if (e.key === "End") setIdx(frames.length);
  };

  const jumpToMs = (ms: number) => {
    let n = 0;
    for (const [i, f] of frames.entries()) if ((f.elapsed_ms ?? 0) <= ms) n = i + 1;
    setIdx(n);
  };

  const nodes: DagNodeSpec[] = useMemo(
    () =>
      rec.steps
        .filter((s) => status[s.key] !== undefined || !s.fan_out_parent)
        .map((s) => ({ key: s.key, type: s.type, depends_on: s.depends_on, name: null })),
    [rec.steps, status],
  );
  const states: Record<string, DagNodeState> = useMemo(() => {
    const out: Record<string, DagNodeState> = {};
    for (const s of rec.steps) {
      if (status[s.key] === undefined) continue;
      const attempts = rec.attempts.filter((a) => a.step_key === s.key && (a.started_ms ?? 0) <= nowMs);
      out[s.key] = { status: status[s.key], attempts: attempts.length, failures: attempts.filter((a) => a.status === "failed" || a.status === "lease_expired").length };
    }
    return out;
  }, [rec, status, nowMs]);

  const focusKey = selected ?? frame?.step_key ?? null;
  const problems = frames.filter((f) => f.severity !== "info");
  const isReplay = rec.run.purpose === "replay";

  return (
    <div className="flex flex-col gap-4 outline-none" tabIndex={0} onKeyDown={onKey} data-testid="flight-player" aria-label="Flight recorder playback. Arrow keys step, space plays.">
      <div role="note" className="flex flex-wrap items-center gap-2 rounded-lg border bg-card p-3 text-sm" data-testid="flight-mode">
        <Badge tone={isReplay ? "warning" : "info"}>{isReplay ? "Deterministic re-execution run" : "Recorded replay"}</Badge>
        <span className="text-muted-foreground">
          {isReplay
            ? <>A side-effect-free re-execution of <Link className="text-primary hover:underline" href={`/flight/${rec.run.replay_of_run_id}`}>{shortId(rec.run.replay_of_run_id)}</Link>: model responses come from its recording, unsafe tools are refused.{" "}
                <Link className="text-primary hover:underline" href={`/flight/compare?a=${rec.run.replay_of_run_id}&b=${rec.run.id}`}>Compare with the original</Link></>
            : "Read-only: this shows immutable stored events. Nothing is executed, no tool runs, no model is called."}
        </span>
      </div>

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold">
            {rec.run.workflow} v{rec.run.workflow_version} <span className="font-mono text-sm text-muted-foreground">{shortId(rec.run.id)}</span>
          </h2>
          <p className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <StatusBadge status={rec.run.status} /> {formatDuration(rec.run.duration_ms)} · {rec.integrity.event_count} events
            <Badge tone={rec.integrity.contiguous ? "success" : "danger"} title={`sha256 ${rec.integrity.sha256}`}>
              {rec.integrity.contiguous ? "event log contiguous" : "GAPS in event log"}
            </Badge>
            · estimated cost {formatCost(rec.run.cost_spent_usd)} (simulated pricing for mock/fixture/recorded providers; price-table estimate for real ones)
            · <Link className="text-primary hover:underline" href={`/runs/${rec.run.id}`}>live run view</Link>
          </p>
        </div>
        <div className="flex gap-2">
          {canWrite ? (
            <>
              <Button variant="outline" size="sm" onClick={() => void exportFlight(rec.run.id)} data-testid="export-flight">
                <Download aria-hidden /> Export sanitized JSON
              </Button>
              {!isReplay ? (
                <Button
                  size="sm"
                  data-testid="reexecute"
                  disabled={reexec.isPending}
                  title="Creates a new run from this recording. Side effects are refused; no paid provider is used."
                  onClick={() => reexec.mutate(undefined, { onSuccess: (r) => router.push(`/flight/${r.id}`) })}
                >
                  <Rewind aria-hidden /> Deterministic re-execution
                </Button>
              ) : null}
            </>
          ) : null}
        </div>
      </div>
      <InlineError error={reexec.error} />

      <Card>
        <CardContent className="flex flex-col gap-3 pt-4">
          <div className="flex flex-wrap items-center gap-2" role="toolbar" aria-label="Playback controls">
            <Button variant="outline" size="icon" aria-label="Jump to start" onClick={() => setIdx(0)}><SkipBack aria-hidden /></Button>
            <Button variant="outline" size="icon" aria-label="Step backward" data-testid="step-back" onClick={() => step(-1)}><ChevronLeft aria-hidden /></Button>
            <Button size="icon" aria-label={active ? "Pause" : "Play"} data-testid="play-pause" onClick={togglePlay}>
              {active ? <Pause aria-hidden /> : <Play aria-hidden />}
            </Button>
            <Button variant="outline" size="icon" aria-label="Step forward" data-testid="step-forward" onClick={() => step(1)}><ChevronRight aria-hidden /></Button>
            <Button variant="outline" size="icon" aria-label="Jump to end" onClick={() => setIdx(frames.length)}><SkipForward aria-hidden /></Button>
            <label className="ml-2 text-xs text-muted-foreground" htmlFor="speed">Speed</label>
            <Select id="speed" className="w-20" value={speed} onChange={(e) => setSpeed(Number(e.target.value))}>
              {SPEEDS.map((s) => <option key={s} value={s}>{s}×</option>)}
            </Select>
            <label className="ml-2 text-xs text-muted-foreground" htmlFor="jump-ms">Jump to (ms)</label>
            <input id="jump-ms" type="number" min={0} max={totalMs} className="h-9 w-24 rounded-md border border-input bg-card px-2 text-sm" placeholder={String(nowMs)}
                   onKeyDown={(e) => { if (e.key === "Enter") jumpToMs(Number((e.target as HTMLInputElement).value)); }} />
            <span className="ml-auto font-mono text-xs text-muted-foreground" data-testid="frame-counter">
              frame {idx}/{frames.length} · t+{formatDuration(nowMs)}
            </span>
          </div>
          <input type="range" min={0} max={frames.length} value={idx} onChange={(e) => setIdx(Number(e.target.value))} className="w-full accent-primary" aria-label="Frame" data-testid="frame-slider" />
          <div className="relative h-5 rounded bg-muted" aria-hidden>
            {frames.map((f, i) => (
              <button key={f.seq} tabIndex={-1} onClick={() => setIdx(i + 1)}
                      className={cn("absolute top-0 h-full w-[3px] rounded-sm", SEVERITY_COLOR[f.severity], f.severity === "info" ? "opacity-60" : "w-[4px]")}
                      style={{ left: `${((f.elapsed_ms ?? 0) / Math.max(1, totalMs)) * 100}%` }} title={`${f.type} @ ${f.elapsed_ms} ms`} />
            ))}
            <div className="absolute top-[-2px] h-[24px] w-[2px] bg-primary" style={{ left: `${(nowMs / Math.max(1, totalMs)) * 100}%` }} />
          </div>
          {problems.length ? (
            <div className="flex flex-wrap items-center gap-1 text-xs" data-testid="problem-frames">
              <TriangleAlert className="size-3.5 text-danger" aria-hidden />
              <span className="text-muted-foreground">Failures and warnings:</span>
              {problems.slice(0, 12).map((f) => (
                <button key={f.seq} onClick={() => { setIdx(frames.indexOf(f) + 1); setSelected(f.step_key); }}
                        className={cn("rounded border px-1.5 py-0.5 font-mono", f.severity === "error" ? "border-danger/50 text-danger" : "border-warning/50 text-warning")}>
                  #{f.seq} {f.type}
                </button>
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <Card>
          <CardHeader><CardTitle>Workflow state at this frame</CardTitle><CardDescription>Statuses are reconstructed from recorded events only.</CardDescription></CardHeader>
          <CardContent>
            <DagGraph nodes={nodes} states={states} selectedKey={focusKey} onSelect={(k) => setSelected(k)} height={380} />
          </CardContent>
        </Card>
        <div className="flex flex-col gap-4">
          <Card>
            <CardHeader><CardTitle>Current frame</CardTitle></CardHeader>
            <CardContent data-testid="current-frame">
              {frame ? (
                <div className="flex flex-col gap-2">
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone={frame.severity === "error" ? "danger" : frame.severity === "warning" ? "warning" : "neutral"}>{frame.type}</Badge>
                    {frame.step_key ? <span className="font-mono text-xs">{frame.step_key}</span> : null}
                    <span className="text-xs text-muted-foreground">seq {frame.seq}{frame.span_id ? ` · span ${frame.span_id}` : ""}</span>
                  </div>
                  <JsonView value={frame.payload} maxHeight="10rem" />
                </div>
              ) : <p className="text-sm text-muted-foreground">Before the first event. Press play or step forward.</p>}
            </CardContent>
          </Card>
          <StepInspector rec={rec} stepKey={focusKey} nowMs={nowMs} status={focusKey ? status[focusKey] : undefined} />
        </div>
      </div>
    </div>
  );
}

function StepInspector({ rec, stepKey, nowMs, status }: { rec: FlightRecording; stepKey: string | null; nowMs: number; status?: string }) {
  const [open, setOpen] = useState<string | null>(null);
  const prev = useRef(stepKey);
  useEffect(() => {
    if (prev.current !== stepKey) { setOpen(null); prev.current = stepKey; }
  }, [stepKey]);
  const detail = useModelCall(rec.run.id, open);
  if (!stepKey) return <Card><CardContent className="pt-4 text-sm text-muted-foreground">Select a node to inspect its state, model calls and tool activity.</CardContent></Card>;
  const step = rec.steps.find((s) => s.key === stepKey);
  const calls = rec.model_calls.filter((c) => c.step_key === stepKey);
  const tools = rec.tool_executions.filter((t) => t.step_key === stepKey);
  const approvals = rec.approvals.filter((a) => a.step_key === stepKey);
  const attempts = rec.attempts.filter((a) => a.step_key === stepKey);
  const future = (ms: number | null) => (ms ?? 0) > nowMs;
  return (
    <Card data-testid="step-inspector">
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><span className="font-mono">{stepKey}</span> {status ? <StatusBadge status={status} /> : null}</CardTitle>
        <CardDescription>{step?.type.replace("_", " ")}{step?.agent_version_id ? ` · agent version ${shortId(step.agent_version_id)}` : ""}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-3 text-sm">
        {step ? <KV items={[["Attempts / failures", `${step.attempts} / ${step.failures}`], ["Duration", formatDuration(step.duration_ms)]]} /> : null}
        {attempts.length ? (
          <div>
            <p className="mb-1 text-xs font-medium uppercase text-muted-foreground">Attempts</p>
            <ul className="flex flex-col gap-1">
              {attempts.map((a) => (
                <li key={a.attempt} className={cn("flex flex-wrap items-center gap-2 text-xs", future(a.started_ms) && "opacity-40")}>
                  <span className="font-mono">#{a.attempt}</span> <StatusBadge status={a.status} /> <span className="text-muted-foreground">{a.worker_id}</span>
                  {a.queue_wait_ms != null ? <span>queued {formatDuration(a.queue_wait_ms)}</span> : null}{a.error_type ? <span className="text-danger">{a.error_type}</span> : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {calls.length ? (
          <div data-testid="inspector-model-calls">
            <p className="mb-1 text-xs font-medium uppercase text-muted-foreground">Model calls (sanitized)</p>
            <ul className="flex flex-col gap-1">
              {calls.map((c) => (
                <li key={c.id} className={cn(future(c.at_ms) && "opacity-40")}>
                  <button className="flex w-full flex-wrap items-center gap-2 rounded border px-2 py-1 text-left text-xs hover:bg-muted" onClick={() => setOpen(open === c.id ? null : c.id)} aria-expanded={open === c.id}>
                    <span className="font-mono">#{c.call_index}</span> <Badge tone={c.status === "succeeded" ? "success" : "danger"}>{c.status}</Badge>
                    <span>{c.provider}/{c.model}</span><span className="text-muted-foreground">{c.latency_ms} ms · {c.input_tokens ?? "?"}→{c.output_tokens ?? "?"} tok</span>
                    {c.tool_calls.length ? <span>→ {c.tool_calls.join(", ")}</span> : null}
                  </button>
                  {open === c.id ? (
                    <div className="mt-1 flex flex-col gap-1">
                      {detail.isLoading ? <p className="text-xs text-muted-foreground">Loading…</p> : null}
                      {detail.data ? <>
                        <p className="text-xs text-muted-foreground">Request{detail.data.truncated ? " (truncated)" : ""} · digest {detail.data.request_digest.slice(0, 12)}</p>
                        <JsonView value={detail.data.request} maxHeight="12rem" />
                        <p className="text-xs text-muted-foreground">Response</p>
                        <JsonView value={detail.data.response ?? detail.data.error} maxHeight="12rem" />
                      </> : null}
                      <InlineError error={detail.error} />
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {tools.length ? (
          <div data-testid="inspector-tools">
            <p className="mb-1 text-xs font-medium uppercase text-muted-foreground">Tool activity</p>
            <ul className="flex flex-col gap-1">
              {tools.map((t) => (
                <li key={t.id} className={cn("rounded border p-2 text-xs", future(t.at_ms) && "opacity-40")}>
                  <div className="flex flex-wrap items-center gap-2"><span className="font-mono">{t.tool}</span><StatusBadge status={t.status} />{t.requires_approval ? <Badge tone="warning">approval</Badge> : null}{t.reason ? <span className="text-muted-foreground">{t.reason}</span> : null}</div>
                  <JsonView value={{ arguments: t.arguments, result: t.result, error: t.error }} maxHeight="8rem" />
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        {approvals.length ? (
          <ul className="flex flex-col gap-1 text-xs">{approvals.map((a) => <li key={a.id} className="flex items-center gap-2"><StatusBadge status={a.status} /> {a.action}</li>)}</ul>
        ) : null}
        {step?.error ? <JsonView value={step.error} maxHeight="8rem" /> : null}
      </CardContent>
    </Card>
  );
}
