"use client";

import { Plus } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { AgentForm } from "@/components/agents/agent-form";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogTrigger } from "@/components/ui/dialog";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { canWrite, useAuth } from "@/lib/auth";
import { formatCost } from "@/lib/format";
import { useAgents } from "@/lib/queries";

export default function AgentsPage() {
  const agents = useAgents();
  const { role } = useAuth();
  const [open, setOpen] = useState(false);
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Agents</h1>
          <p className="text-sm text-muted-foreground">Provider, model, allowed tools and execution limits per agent.</p>
        </div>
        {canWrite(role) ? (
          <Dialog open={open} onOpenChange={setOpen}>
            <DialogTrigger asChild><Button><Plus aria-hidden /> New agent</Button></DialogTrigger>
            <DialogContent title="Create agent" className="w-[min(820px,calc(100vw-2rem))]">
              <AgentForm onSaved={() => setOpen(false)} />
            </DialogContent>
          </Dialog>
        ) : null}
      </div>
      {agents.isLoading ? <LoadingBlock rows={4} /> : agents.error ? <ErrorState error={agents.error} onRetry={() => void agents.refetch()} /> :
        agents.data?.items.length ? (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {agents.data.items.map((a) => (
              <Link key={a.id} href={`/agents/${a.id}`} className="group">
                <Card className="h-full p-4 transition-colors group-hover:border-primary/50">
                  <div className="flex items-start justify-between gap-2">
                    <p className="font-medium">{a.name}</p>
                    <Badge tone={a.provider === "mock" ? "warning" : "primary"}>{a.provider}</Badge>
                  </div>
                  <p className="mt-1 font-mono text-xs text-muted-foreground">{a.model}</p>
                  <p className="mt-2 line-clamp-2 text-sm text-muted-foreground">{a.description}</p>
                  <div className="mt-3 flex flex-wrap gap-1">
                    {a.allowed_tool_ids.length ? a.allowed_tool_ids.map((t) => <Badge key={t}>{t}</Badge>) : <span className="text-xs text-muted-foreground">no tools</span>}
                  </div>
                  <p className="mt-3 text-xs text-muted-foreground">max {a.max_steps} steps · {formatCost(a.max_cost_usd)} · {a.timeout_seconds}s timeout</p>
                </Card>
              </Link>
            ))}
          </div>
        ) : <EmptyState title="No agents" />}
    </div>
  );
}
