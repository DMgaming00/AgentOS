"use client";

import { useRouter, useSearchParams } from "next/navigation";

import { RunsTable } from "@/components/runs/runs-table";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { Select } from "@/components/ui/inputs";
import { useRuns } from "@/lib/queries";

const STATUSES = ["", "running", "waiting_approval", "planning", "succeeded", "failed", "cancelled"];
const PAGE = 25;

export function RunsList() {
  const params = useSearchParams();
  const router = useRouter();
  const status = params.get("status") ?? "";
  const offset = Number(params.get("offset") ?? 0);
  const runs = useRuns({ status: status || undefined, limit: PAGE, offset });
  const set = (k: string, v: string) => {
    const p = new URLSearchParams(params.toString());
    if (v) p.set(k, v);
    else p.delete(k);
    if (k !== "offset") p.delete("offset");
    router.replace(`/runs?${p}`);
  };
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Runs</h1>
          <p className="text-sm text-muted-foreground">{runs.data ? `${runs.data.total} runs` : " "}</p>
        </div>
        <div className="w-52">
          <label htmlFor="status-filter" className="sr-only">Filter by status</label>
          <Select id="status-filter" value={status} onChange={(e) => set("status", e.target.value)}>
            {STATUSES.map((s) => <option key={s} value={s}>{s ? s.replace("_", " ") : "All statuses"}</option>)}
          </Select>
        </div>
      </div>
      <Card>
        <CardContent className="pt-4">
          {runs.isLoading ? <LoadingBlock rows={6} /> : runs.error ? <ErrorState error={runs.error} onRetry={() => void runs.refetch()} /> :
            runs.data?.items.length ? <RunsTable runs={runs.data.items} /> : <EmptyState title="No runs match" />}
          {runs.data && runs.data.total > PAGE ? (
            <div className="mt-4 flex items-center justify-end gap-2 text-sm">
              <Button size="sm" variant="outline" disabled={offset === 0} onClick={() => set("offset", String(Math.max(0, offset - PAGE)))}>Previous</Button>
              <span className="text-muted-foreground">{offset + 1}–{Math.min(offset + PAGE, runs.data.total)} of {runs.data.total}</span>
              <Button size="sm" variant="outline" disabled={offset + PAGE >= runs.data.total} onClick={() => set("offset", String(offset + PAGE))}>Next</Button>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  );
}
