/** Paired difference with its 95% CI against 0 (single axis, -1..+1 success-rate difference). */
export function CIChart({ diff, ci, n }: { diff: number | null; ci: [number, number] | null; n: number }) {
  if (diff == null || !ci) return <p className="text-sm text-muted-foreground">No matched cases.</p>;
  const x = (v: number) => 20 + ((v + 1) / 2) * 560;
  const color = ci[0] > 0 ? "var(--success)" : ci[1] < 0 ? "var(--danger)" : "var(--warning)";
  return (
    <figure className="flex flex-col gap-1" data-testid="ci-chart">
      <svg viewBox="0 0 600 70" className="w-full" role="img"
           aria-label={`Paired success difference ${diff.toFixed(3)}, 95% CI ${ci[0].toFixed(3)} to ${ci[1].toFixed(3)}, n=${n}`}>
        <line x1={20} x2={580} y1={40} y2={40} stroke="var(--border)" />
        {[-1, -0.5, 0, 0.5, 1].map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={t === 0 ? 10 : 36} y2={t === 0 ? 60 : 44} stroke={t === 0 ? "var(--muted-foreground)" : "var(--border)"}
                  strokeDasharray={t === 0 ? "3 3" : undefined} />
            <text x={x(t)} y={68} textAnchor="middle" fontSize={10} fill="var(--muted-foreground)">{t > 0 ? `+${t}` : t}</text>
          </g>
        ))}
        <line x1={x(ci[0])} x2={x(ci[1])} y1={40} y2={40} stroke={color} strokeWidth={4} strokeLinecap="round" />
        <circle cx={x(diff)} cy={40} r={6} fill={color} stroke="var(--card)" strokeWidth={2} />
        <text x={x(diff)} y={24} textAnchor="middle" fontSize={11} fill="var(--foreground)">
          {diff >= 0 ? "+" : ""}{diff.toFixed(3)} [{ci[0].toFixed(3)}, {ci[1].toFixed(3)}]
        </text>
      </svg>
      <figcaption className="text-xs text-muted-foreground">
        Candidate minus baseline success rate per matched case; percentile bootstrap 95% CI, n = {n}.
      </figcaption>
    </figure>
  );
}
