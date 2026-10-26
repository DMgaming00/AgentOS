"use client";

import { useQuery } from "@tanstack/react-query";
import { Download, Eye } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { downloadArtifact, rawFetch } from "@/lib/api";
import { formatTime } from "@/lib/format";
import type { Artifact } from "@/types";

export function ArtifactPreview({ artifact, onClose }: { artifact: Artifact; onClose: () => void }) {
  const { data, error, isLoading } = useQuery({
    queryKey: ["artifact-content", artifact.id],
    queryFn: async () => (await rawFetch(`/api/v1/artifacts/${artifact.id}/content`)).text(),
  });
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent title={artifact.filename} description={`${artifact.content_type} · ${artifact.byte_size} bytes · sha256 ${artifact.sha256.slice(0, 12)}…`} className="w-[min(900px,calc(100vw-2rem))]">
        {isLoading ? <LoadingBlock /> : error ? <ErrorState error={error} /> : (
          <pre className="max-h-[65vh] overflow-auto whitespace-pre-wrap rounded-md border bg-muted/50 p-4 text-xs" data-testid="artifact-preview">{data}</pre>
        )}
      </DialogContent>
    </Dialog>
  );
}

export function ArtifactsList({ artifacts }: { artifacts: Artifact[] }) {
  const [preview, setPreview] = useState<Artifact | null>(null);
  const [error, setError] = useState<unknown>(null);
  if (!artifacts.length) return <p className="text-sm text-muted-foreground">No artifacts yet.</p>;
  return (
    <ul className="flex flex-col gap-2" data-testid="artifacts-list">
      {artifacts.map((a) => (
        <li key={a.id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3">
          <div className="min-w-0">
            <p className="truncate font-medium">{a.filename}</p>
            <p className="text-xs text-muted-foreground">
              {a.content_type} · {a.byte_size.toLocaleString()} bytes · {formatTime(a.created_at)}
            </p>
          </div>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" onClick={() => setPreview(a)}>
              <Eye aria-hidden /> Preview
            </Button>
            <Button
              size="sm"
              onClick={() => downloadArtifact(a.id, a.filename).catch(setError)}
              data-testid={`download-${a.filename}`}
            >
              <Download aria-hidden /> Download
            </Button>
          </div>
        </li>
      ))}
      {error ? <ErrorState error={error} /> : null}
      {preview ? <ArtifactPreview artifact={preview} onClose={() => setPreview(null)} /> : null}
    </ul>
  );
}
