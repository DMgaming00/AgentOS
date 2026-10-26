"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { useRef, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorState, InlineError, LoadingBlock } from "@/components/ui/feedback";
import { Field, Input } from "@/components/ui/inputs";
import { KV } from "@/components/ui/json";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { ApiError, api, rawFetch, unwrap } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { formatTime } from "@/lib/format";
import { useDocuments, useSystem, useWorkspace } from "@/lib/queries";

export default function SettingsPage() {
  const system = useSystem();
  const ws = useWorkspace();
  const { role } = useAuth();
  if (system.isLoading || ws.isLoading) return <LoadingBlock rows={6} />;
  if (system.error || ws.error) return <ErrorState error={system.error ?? ws.error} />;
  const s = system.data!;
  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-xl font-semibold">Settings</h1>
      <div className="grid gap-4 xl:grid-cols-2">
        <WorkspaceSettings canEdit={role === "admin"} name={ws.data!.name} maxCost={Number((ws.data!.settings as { max_run_cost_usd?: number }).max_run_cost_usd ?? s.limits.max_run_cost_usd)} members={ws.data!.members as { email: string; role: string }[]} />
        <Card>
          <CardHeader><CardTitle>Model providers</CardTitle><CardDescription>Keys are read server-side from the environment and never sent to the browser.</CardDescription></CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Table>
              <THead><tr><TH>Provider</TH><TH>Status</TH><TH>Default model</TH></tr></THead>
              <TBody>
                {Object.entries(s.providers).map(([name, p]) => {
                  const info = p as { configured?: boolean; default_model?: string | null; simulated?: boolean };
                  return (
                    <TR key={name}>
                      <TD className="font-medium">{name}</TD>
                      <TD>{info.simulated ? <Badge tone="warning">always available (simulated)</Badge> : info.configured ? <Badge tone="success">API key configured</Badge> : <Badge>not configured</Badge>}</TD>
                      <TD className="font-mono text-xs">{info.default_model ?? "not set"}</TD>
                    </TR>
                  );
                })}
              </TBody>
            </Table>
            <div className="rounded-md border bg-muted/40 p-3 text-xs leading-relaxed text-muted-foreground">
              <p className="mb-1 font-medium text-foreground">API key setup</p>
              <ol className="list-decimal pl-4">
                <li>Add <code>ANTHROPIC_API_KEY=…</code> and/or <code>OPENAI_API_KEY=…</code> to <code>.env</code> (never commit it).</li>
                <li>Set <code>AGENTOS_OPENAI_MODEL</code> to a model ID you have access to (no default is shipped).</li>
                <li>Optionally set <code>AGENTOS_PLANNER_PROVIDER=anthropic</code> and re-seed with <code>AGENTOS_DEMO_PROVIDER=anthropic</code>.</li>
                <li>Restart: <code>docker compose up -d --force-recreate api worker</code>. A failing real provider is reported as an error, never replaced by the mock.</li>
              </ol>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Budget policy and engine</CardTitle></CardHeader>
          <CardContent>
            <KV items={[
              ["Planner provider", s.planner_provider],
              ["Max run cost (system cap)", `$${s.limits.max_run_cost_usd}`],
              ["Max DAG nodes", String(s.limits.max_dag_nodes)],
              ["Approval TTL", `${Number(s.limits.approval_ttl_seconds) / 3600} h`],
              ["Lease TTL / heartbeat", `${s.engine.lease_ttl_seconds}s / ${s.engine.heartbeat_interval_seconds}s`],
              ["Worker concurrency", String(s.engine.worker_concurrency)],
              ["Delivery semantics", String(s.engine.delivery)],
              ["Pricing table", `${s.pricing.version} — ${s.pricing.source}`],
            ]} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Local development</CardTitle></CardHeader>
          <CardContent>
            <KV items={[
              ["Version", s.version], ["Environment", s.environment],
              ["Dev authentication", s.dev_auth_enabled ? "enabled (seeded users)" : "disabled"],
              ["Redis", s.redis_available ? "available" : "unavailable (polling fallback)"],
              ["OpenTelemetry export", s.otel_enabled ? "enabled" : "disabled (trace ids still recorded on events)"],
              ["API docs", <a key="d" className="text-primary hover:underline" href={`${process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000"}/docs`} target="_blank" rel="noreferrer">OpenAPI / Swagger UI</a>],
            ]} />
          </CardContent>
        </Card>
      </div>
      <Documents canUpload={role === "admin" || role === "developer"} />
    </div>
  );
}

function WorkspaceSettings({ canEdit, name, maxCost, members }: { canEdit: boolean; name: string; maxCost: number; members: { email: string; role: string }[] }) {
  const qc = useQueryClient();
  const [n, setN] = useState(name);
  const [cost, setCost] = useState(String(maxCost));
  const save = useMutation({
    mutationFn: async () => unwrap(await api.PATCH("/api/v1/workspace", { body: { name: n, max_run_cost_usd: Number(cost) } })),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["workspace"] }),
  });
  return (
    <Card>
      <CardHeader><CardTitle>Workspace</CardTitle><CardDescription>{canEdit ? "Admins can edit." : "Only admins can edit workspace settings."}</CardDescription></CardHeader>
      <CardContent className="flex flex-col gap-3">
        <Field label="Name" htmlFor="ws-name"><Input id="ws-name" value={n} onChange={(e) => setN(e.target.value)} disabled={!canEdit} /></Field>
        <Field label="Max cost per run (USD)" htmlFor="ws-cost" hint="Workflow budgets above this are rejected at validation.">
          <Input id="ws-cost" type="number" step="0.5" min={0} value={cost} onChange={(e) => setCost(e.target.value)} disabled={!canEdit} />
        </Field>
        {canEdit ? <Button onClick={() => save.mutate()} disabled={save.isPending} className="self-start">Save</Button> : null}
        <InlineError error={save.error} />
        <p className="text-xs font-medium text-muted-foreground">Members</p>
        <ul className="text-sm">{members.map((m) => <li key={m.email}>{m.email} <Badge>{m.role}</Badge></li>)}</ul>
      </CardContent>
    </Card>
  );
}

function Documents({ canUpload }: { canUpload: boolean }) {
  const docs = useDocuments();
  const qc = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData();
      form.append("file", file);
      const res = await rawFetch("/api/v1/documents", { method: "POST", body: form }).catch((e: unknown) => { throw e instanceof ApiError ? e : new Error(String(e)); });
      return res.json();
    },
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["documents"] }),
  });
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <div><CardTitle>Workspace documents</CardTitle><CardDescription>Untrusted source material searched by document_search (.md .txt .pdf .csv .json).</CardDescription></div>
        {canUpload ? (
          <>
            <input ref={input} type="file" className="hidden" accept=".md,.txt,.pdf,.csv,.json" aria-label="Upload document"
                   onChange={(e) => { const f = e.target.files?.[0]; if (f) upload.mutate(f); e.target.value = ""; }} />
            <Button size="sm" onClick={() => input.current?.click()} disabled={upload.isPending}><Upload aria-hidden /> Upload</Button>
          </>
        ) : null}
      </CardHeader>
      <CardContent>
        <InlineError error={upload.error} />
        {docs.isLoading ? <LoadingBlock /> : (
          <Table>
            <THead><tr><TH>File</TH><TH>Title</TH><TH>Type</TH><TH>Chunks</TH><TH>Size</TH><TH>Added</TH></tr></THead>
            <TBody>{docs.data?.items.map((d) => (
              <TR key={d.id}><TD className="font-mono text-xs">{d.filename}</TD><TD>{d.title}</TD><TD className="text-xs">{d.content_type}</TD>
                <TD>{d.chunk_count}</TD><TD>{d.byte_size.toLocaleString()} B</TD><TD className="text-muted-foreground">{formatTime(d.created_at)}</TD></TR>))}</TBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
