import { cn } from "@/lib/utils";

export function JsonView({ value, className, maxHeight = "24rem" }: { value: unknown; className?: string; maxHeight?: string }) {
  if (value === null || value === undefined) return <p className="text-xs text-muted-foreground">—</p>;
  return (
    <pre
      className={cn("overflow-auto rounded-md border bg-muted/60 p-3 font-mono text-xs leading-relaxed", className)}
      style={{ maxHeight }}
    >
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function KV({ items }: { items: [string, React.ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1.5 text-sm">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted-foreground">{k}</dt>
          <dd className="min-w-0 break-words">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
