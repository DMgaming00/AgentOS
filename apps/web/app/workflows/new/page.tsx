import { Suspense } from "react";

import { LoadingBlock } from "@/components/ui/feedback";
import { NewWorkflow } from "./new-workflow";

export const metadata = { title: "New workflow" };

export default function NewWorkflowPage() {
  return (
    <div className="flex flex-col gap-4">
      <div>
        <h1 className="text-xl font-semibold">New workflow</h1>
        <p className="text-sm text-muted-foreground">
          Plans from the model are validated with the same policy as hand-written DAGs: cycles, dependencies, tool permissions and budgets.
        </p>
      </div>
      <Suspense fallback={<LoadingBlock rows={6} />}>
        <NewWorkflow />
      </Suspense>
    </div>
  );
}
