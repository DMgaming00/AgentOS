import { Suspense } from "react";

import { LoadingBlock } from "@/components/ui/feedback";
import { RunDetailView } from "@/components/runs/run-detail";

export const metadata = { title: "Run" };

export default function RunPage() {
  return (
    <Suspense fallback={<LoadingBlock rows={8} label="Loading run" />}>
      <RunDetailView />
    </Suspense>
  );
}
