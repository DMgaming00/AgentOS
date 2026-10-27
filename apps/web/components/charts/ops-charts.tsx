"use client";

import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { Bucket } from "@/types/ops";

const AXIS = { stroke: "var(--muted-foreground)", fontSize: 11, tickLine: false, axisLine: false } as const;
const TOOLTIP = {
  contentStyle: { background: "var(--card)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12, color: "var(--foreground)" },
  labelStyle: { color: "var(--muted-foreground)" },
  cursor: { fill: "var(--muted)", opacity: 0.4 },
} as const;

/** Latency distribution: one bar per bucket (upper bound on the axis), with a table view of the same counts. */
export function HistogramChart({ buckets, unit, label, height = 180 }: { buckets: Bucket[]; unit: string; label: string; height?: number }) {
  const data = buckets.map((b, i) => ({ bucket: b.le == null ? `>${buckets[i - 1]?.le ?? ""}` : `≤${b.le}`, count: b.count }));
  const total = buckets.reduce((a, b) => a + b.count, 0);
  if (!total) return <p className="text-sm text-muted-foreground">No samples in this window.</p>;
  return (
    <div role="img" aria-label={`${label} distribution in ${unit}`}>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ left: 0, right: 8, top: 8, bottom: 4 }}>
          <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
          <XAxis dataKey="bucket" {...AXIS} interval={0} fontSize={10} />
          <YAxis {...AXIS} allowDecimals={false} width={32} />
          <Tooltip {...TOOLTIP} formatter={(v) => [String(v), `${label} (${unit})`]} />
          <Bar dataKey="count" fill="var(--series-1)" radius={[4, 4, 0, 0]} maxBarSize={32} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
      <details className="mt-1 text-xs">
        <summary className="cursor-pointer text-muted-foreground">View as table</summary>
        <table className="mt-1 w-full text-left"><tbody>{data.map((d) => <tr key={d.bucket}><td className="pr-3">{d.bucket} {unit}</td><td>{d.count}</td></tr>)}</tbody></table>
      </details>
    </div>
  );
}

/** Agent x error-type counts. One hue, intensity by count; every cell prints its number (no color-only encoding). */
export function Heatmap({ cells, rowKey, colKey }: { cells: { row: string; col: string; count: number }[]; rowKey: string; colKey: string }) {
  const rows = Array.from(new Set(cells.map((c) => c.row)));
  const cols = Array.from(new Set(cells.map((c) => c.col)));
  const max = Math.max(1, ...cells.map((c) => c.count));
  const at = (r: string, c: string) => cells.find((x) => x.row === r && x.col === c)?.count ?? 0;
  if (!cells.length) return <p className="text-sm text-muted-foreground">No failures in this window.</p>;
  return (
    <div className="overflow-x-auto" role="table" aria-label={`${rowKey} by ${colKey}`} data-testid="failure-heatmap">
      <div role="row" className="grid gap-1 text-[11px] text-muted-foreground" style={{ gridTemplateColumns: `160px repeat(${cols.length}, minmax(84px, 1fr))` }}>
        <span role="columnheader">{rowKey} \ {colKey}</span>
        {cols.map((c) => <span key={c} role="columnheader" className="truncate" title={c}>{c}</span>)}
        {rows.map((r) => (
          <div key={r} role="row" className="contents">
            <span role="rowheader" className="truncate text-foreground" title={r}>{r}</span>
            {cols.map((c) => {
              const n = at(r, c);
              return <span key={c} role="cell" title={`${r} · ${c}: ${n}`} className="rounded px-2 py-1 text-center text-xs text-foreground" style={{ background: n ? `color-mix(in srgb, var(--series-1) ${15 + Math.round((n / max) * 70)}%, transparent)` : "transparent", border: "1px solid var(--chart-grid)" }}>{n || "·"}</span>;
            })}
          </div>))}
      </div>
    </div>
  );
}

/** Which agent calls which tool, how often (edge width) and how often it failed (second color, and a number). */
export function DependencyGraph({ edges }: { edges: { agent: string; tool: string; calls: number; failed: number }[] }) {
  const agents = Array.from(new Set(edges.map((e) => e.agent)));
  const tools = Array.from(new Set(edges.map((e) => e.tool)));
  if (!edges.length) return <p className="text-sm text-muted-foreground">No tool calls by agents in this window.</p>;
  const H = Math.max(agents.length, tools.length) * 44 + 24;
  const y = (i: number, n: number) => 24 + i * ((H - 48) / Math.max(1, n - 1 || 1)) + (n === 1 ? (H - 48) / 2 : 0);
  const max = Math.max(...edges.map((e) => e.calls));
  return (
    <div data-testid="dependency-graph" role="img" aria-label="Agent to tool dependency graph">
      <svg viewBox={`0 0 560 ${H}`} className="w-full">
        {edges.map((e) => {
          const y1 = y(agents.indexOf(e.agent), agents.length), y2 = y(tools.indexOf(e.tool), tools.length);
          const w = 1 + (e.calls / max) * 6;
          return (
            <g key={`${e.agent}-${e.tool}`}>
              <path d={`M170 ${y1} C 280 ${y1}, 280 ${y2}, 390 ${y2}`} fill="none" stroke="var(--series-1)" strokeOpacity={0.55} strokeWidth={w}><title>{`${e.agent} → ${e.tool}: ${e.calls} calls, ${e.failed} failed or denied`}</title></path>
              {e.failed ? <path d={`M170 ${y1} C 280 ${y1}, 280 ${y2}, 390 ${y2}`} fill="none" stroke="var(--series-2)" strokeWidth={Math.max(1, (e.failed / max) * 6)} strokeDasharray="4 3" /> : null}
            </g>);
        })}
        {agents.map((a, i) => <g key={a}><rect x={10} y={y(i, agents.length) - 14} width={160} height={28} rx={6} fill="var(--card)" stroke="var(--border)" /><text x={20} y={y(i, agents.length) + 4} fontSize={11} fill="var(--foreground)">{a.slice(0, 24)}</text></g>)}
        {tools.map((t, i) => <g key={t}><rect x={390} y={y(i, tools.length) - 14} width={160} height={28} rx={6} fill="var(--card)" stroke="var(--border)" /><text x={400} y={y(i, tools.length) + 4} fontSize={11} fill="var(--foreground)">{t}</text></g>)}
      </svg>
      <details className="text-xs"><summary className="cursor-pointer text-muted-foreground">View as table</summary>
        <table className="mt-1 w-full text-left"><thead className="text-muted-foreground"><tr><th>Agent</th><th>Tool</th><th>Calls</th><th>Failed / denied</th></tr></thead><tbody>{edges.map((e) => <tr key={`${e.agent}-${e.tool}`}><td>{e.agent}</td><td>{e.tool}</td><td>{e.calls}</td><td>{e.failed}</td></tr>)}</tbody></table>
      </details>
      <p className="text-[11px] text-muted-foreground">Edge width = calls; dashed orange overlay = failed or denied calls.</p>
    </div>
  );
}
