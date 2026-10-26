import { Suspense } from "react";

import { LoadingBlock } from "@/components/ui/feedback";
import { WorkflowDetailView } from "@/components/workflows/workflow-detail";

export const metadata = { title: "Workflow" };

export default function WorkflowPage() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <WorkflowDetailView />
    </Suspense>
  );
}
