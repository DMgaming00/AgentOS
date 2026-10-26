import { cn } from "@/lib/utils";

type Change = { field: string; before?: unknown; after?: unknown; added?: string[]; removed?: string[] };

function show(v: unknown): string {
  if (v === undefined) return "—";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

/** Field-level configuration diff (baseline -> candidate). */
export function DiffView({ diff }: { diff: Change[] }) {
  if (!diff.length) return <p className="text-sm text-muted-foreground">No configuration change.</p>;
  return (
    <div className="flex flex-col gap-3" data-testid="config-diff">
      {diff.map((d) => (
        <div key={d.field} className="rounded-md border text-xs">
          <div className="border-b bg-muted/50 px-3 py-1.5 font-mono font-medium">{d.field}</div>
          {d.added || d.removed ? (
            <div className="flex flex-wrap gap-2 p-3">
              {(d.removed ?? []).map((x) => <span key={`r${x}`} className="rounded bg-danger/10 px-2 py-0.5 text-danger line-through">{x}</span>)}
              {(d.added ?? []).map((x) => <span key={`a${x}`} className="rounded bg-success/10 px-2 py-0.5 text-success">+ {x}</span>)}
              {!d.added?.length && !d.removed?.length ? <span className="text-muted-foreground">reordered</span> : null}
            </div>
          ) : (
            <div className="grid gap-px bg-border md:grid-cols-2">
              <pre className={cn("whitespace-pre-wrap break-words bg-danger/5 p-3")}><span className="mr-2 select-none text-danger">−</span>{show(d.before)}</pre>
              <pre className={cn("whitespace-pre-wrap break-words bg-success/5 p-3")}><span className="mr-2 select-none text-success">+</span>{show(d.after)}</pre>
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
