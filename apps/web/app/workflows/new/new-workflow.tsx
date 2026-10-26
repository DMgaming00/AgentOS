"use client";

import { useSearchParams } from "next/navigation";

import { ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { WorkflowEditor } from "@/components/workflows/workflow-editor";
import { useWorkflow } from "@/lib/queries";

export function NewWorkflow() {
  const from = useSearchParams().get("from");
  if (!from) return <WorkflowEditor />;
  return <FromExisting id={from} />;
}

function FromExisting({ id }: { id: string }) {
  const wf = useWorkflow(id);
  if (wf.isLoading) return <LoadingBlock rows={6} />;
  if (wf.error || !wf.data) return <ErrorState error={wf.error} />;
  return <WorkflowEditor initial={{ name: wf.data.name, description: wf.data.description, definition: wf.data.definition }} />;
}
