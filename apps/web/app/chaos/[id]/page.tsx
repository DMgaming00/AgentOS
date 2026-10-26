import { Suspense } from "react";

import { ExperimentView } from "@/components/chaos/experiment-view";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Chaos experiment" };

export default function ExperimentPage() {
  return (
    <Suspense fallback={<LoadingBlock rows={8} />}>
      <ExperimentView />
    </Suspense>
  );
}
