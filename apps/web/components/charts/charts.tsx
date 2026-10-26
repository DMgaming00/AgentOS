"use client";

import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

/**
 * Chart conventions (see dataviz guidance): one y-axis per chart, thin marks, 4px rounded
 * bar ends, 2px lines, recessive grid, legend for >= 2 series, tooltips on hover,
 * text in text tokens (never series color), and a table view for every chart.
 */
const AXIS = { stroke: "var(--muted-foreground)", fontSize: 11, tickLine: false, axisLine: false } as const;
// Legend text stays in the text token; the swatch beside it carries the series identity.
const legendText = (value: string) => <span style={{ color: "var(--muted-foreground)" }}>{value}</span>;

const TOOLTIP = {
  contentStyle: { background: "var(--card)", border: "1px solid var(--border)", borderRadius: 6, fontSize: 12, color: "var(--foreground)" },
  labelStyle: { color: "var(--muted-foreground)" },
  cursor: { fill: "var(--muted)", opacity: 0.4 },
} as const;

export type Datum = Record<string, string | number | null>;

export function TableView({ data, columns }: { data: Datum[]; columns: { key: string; label: string; format?: (v: unknown) => string }[] }) {
  return (
    <details className="mt-2 text-xs">
      <summary className="cursor-pointer text-muted-foreground">View as table</summary>
      <table className="mt-2 w-full text-left">
        <thead className="text-muted-foreground"><tr>{columns.map((c) => <th key={c.key} className="py-1 pr-3 font-medium">{c.label}</th>)}</tr></thead>
        <tbody>
          {data.map((d, i) => (
            <tr key={i} className="border-t">
              {columns.map((c) => <td key={c.key} className="py-1 pr-3 tabular-nums">{c.format ? c.format(d[c.key]) : String(d[c.key] ?? "—")}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </details>
  );
}

export function HBarChart({
  data, category, value, valueLabel, format = (v) => String(v), color = "var(--series-1)", height,
}: {
  data: Datum[]; category: string; value: string; valueLabel: string; format?: (v: number) => string; color?: string; height?: number;
}) {
  const h = height ?? Math.max(120, data.length * 34 + 30);
  return (
    <div role="img" aria-label={`${valueLabel} by ${category}`}>
      <ResponsiveContainer width="100%" height={h}>
        <BarChart data={data} layout="vertical" margin={{ left: 8, right: 24, top: 4, bottom: 4 }} barCategoryGap={6}>
          <CartesianGrid horizontal={false} stroke="var(--chart-grid)" />
          <XAxis type="number" {...AXIS} tickFormatter={(v) => format(Number(v))} />
          <YAxis type="category" dataKey={category} {...AXIS} width={130} />
          <Tooltip {...TOOLTIP} formatter={(v) => [format(Number(v)), valueLabel]} />
          <Bar dataKey={value} fill={color} radius={[0, 4, 4, 0]} maxBarSize={18} isAnimationActive={false} />
        </BarChart>
      </ResponsiveContainer>
      <TableView data={data} columns={[{ key: category, label: category }, { key: value, label: valueLabel, format: (v) => format(Number(v)) }]} />
    </div>
  );
}

export function StackedBarChart({
  data, category, series, height = 220,
}: {
  data: Datum[]; category: string; series: { key: string; label: string; color: string }[]; height?: number;
}) {
  return (
    <div role="img" aria-label={`Stacked counts by ${category}`}>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ left: 0, right: 8, top: 8, bottom: 4 }}>
          <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
          <XAxis dataKey={category} {...AXIS} />
          <YAxis {...AXIS} allowDecimals={false} width={36} />
          <Tooltip {...TOOLTIP} />
          <Legend wrapperStyle={{ fontSize: 12 }} formatter={legendText} />
          {series.map((s, i) => (
            <Bar key={s.key} dataKey={s.key} name={s.label} stackId="a" fill={s.color} stroke="var(--card)" strokeWidth={2}
                 radius={i === series.length - 1 ? [4, 4, 0, 0] : 0} maxBarSize={36} isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <TableView data={data} columns={[{ key: category, label: category }, ...series.map((s) => ({ key: s.key, label: s.label }))]} />
    </div>
  );
}

export function SeriesLineChart({
  data, x, series, format = (v) => String(v), height = 220,
}: {
  data: Datum[]; x: string; series: { key: string; label: string; color: string }[]; format?: (v: number) => string; height?: number;
}) {
  return (
    <div role="img" aria-label="Line chart">
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data} margin={{ left: 0, right: 16, top: 8, bottom: 4 }}>
          <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
          <XAxis dataKey={x} {...AXIS} />
          <YAxis {...AXIS} tickFormatter={(v) => format(Number(v))} width={56} />
          <Tooltip {...TOOLTIP} cursor={{ stroke: "var(--muted-foreground)", strokeDasharray: "3 3" }} formatter={(v, n) => [format(Number(v)), n]} />
          <Legend wrapperStyle={{ fontSize: 12 }} formatter={legendText} />
          {series.map((s) => (
            <Line key={s.key} dataKey={s.key} name={s.label} stroke={s.color} strokeWidth={2} dot={{ r: 4, strokeWidth: 2, stroke: "var(--card)" }}
                  activeDot={{ r: 5 }} isAnimationActive={false} connectNulls />
          ))}
        </LineChart>
      </ResponsiveContainer>
      <TableView data={data} columns={[{ key: x, label: x }, ...series.map((s) => ({ key: s.key, label: s.label, format: (v: unknown) => (v == null ? "—" : format(Number(v))) }))]} />
    </div>
  );
}

/** Side-by-side bars for two or three series sharing one unit (one y-axis). */
export function GroupedBarChart({
  data, category, series, format = (v) => String(v), height = 220, domain,
}: {
  data: Datum[]; category: string; series: { key: string; label: string; color: string }[];
  format?: (v: number) => string; height?: number; domain?: [number, number];
}) {
  return (
    <div role="img" aria-label={`Grouped comparison by ${category}`}>
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={data} margin={{ left: 0, right: 8, top: 16, bottom: 4 }} barGap={2}>
          <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
          <XAxis dataKey={category} {...AXIS} />
          <YAxis {...AXIS} tickFormatter={(v) => format(Number(v))} width={76} domain={domain} />
          <Tooltip {...TOOLTIP} formatter={(v, n) => [v == null ? "n/a" : format(Number(v)), n]} />
          <Legend wrapperStyle={{ fontSize: 12 }} formatter={legendText} />
          {series.map((s) => (
            <Bar key={s.key} dataKey={s.key} name={s.label} fill={s.color} radius={[4, 4, 0, 0]} maxBarSize={32}
                 isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <TableView data={data} columns={[{ key: category, label: category },
        ...series.map((s) => ({ key: s.key, label: s.label, format: (v: unknown) => (v == null ? "n/a" : format(Number(v))) }))]} />
    </div>
  );
}
