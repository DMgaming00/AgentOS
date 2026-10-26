"use client";

import { Download, Eye } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ArtifactPreview } from "@/components/runs/artifacts-list";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { EmptyState, ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { downloadArtifact } from "@/lib/api";
import { formatTime, shortId } from "@/lib/format";
import { useArtifacts } from "@/lib/queries";
import type { Artifact } from "@/types";

export default function ArtifactsPage() {
  const artifacts = useArtifacts();
  const [preview, setPreview] = useState<Artifact | null>(null);
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">Artifacts</h1>
        <p className="text-sm text-muted-foreground">Files written by the approval-gated artifact_writer tool. Content is stored outside the database and verified by SHA-256.</p>
      </div>
      <Card>
        <CardContent className="pt-4">
          {artifacts.isLoading ? <LoadingBlock /> : artifacts.error ? <ErrorState error={artifacts.error} /> : artifacts.data?.items.length ? (
            <Table>
              <THead><tr><TH>File</TH><TH>Run</TH><TH>Type</TH><TH>Size</TH><TH>SHA-256</TH><TH>Created</TH><TH /></tr></THead>
              <TBody>
                {artifacts.data.items.map((a) => (
                  <TR key={a.id}>
                    <TD className="font-medium">{a.filename}</TD>
                    <TD><Link className="font-mono text-primary hover:underline" href={`/runs/${a.run_id}`}>{shortId(a.run_id)}</Link></TD>
                    <TD className="text-xs">{a.content_type}</TD>
                    <TD className="tabular-nums">{a.byte_size.toLocaleString()} B</TD>
                    <TD className="font-mono text-xs text-muted-foreground">{a.sha256.slice(0, 12)}…</TD>
                    <TD className="text-muted-foreground">{formatTime(a.created_at)}</TD>
                    <TD className="flex justify-end gap-2">
                      <Button size="sm" variant="outline" onClick={() => setPreview(a)} aria-label={`Preview ${a.filename}`}><Eye /></Button>
                      <Button size="sm" onClick={() => void downloadArtifact(a.id, a.filename)} aria-label={`Download ${a.filename}`}><Download /></Button>
                    </TD>
                  </TR>
                ))}
              </TBody>
            </Table>
          ) : <EmptyState title="No artifacts yet" description="Approve an export in a run to create one." />}
        </CardContent>
      </Card>
      {preview ? <ArtifactPreview artifact={preview} onClose={() => setPreview(null)} /> : null}
    </div>
  );
}
