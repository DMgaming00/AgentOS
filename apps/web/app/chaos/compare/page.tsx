import { Suspense } from "react";

import { ChaosCompareView } from "@/components/chaos/compare-view";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Reliability comparison" };

export default function ChaosComparePage() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <ChaosCompareView />
    </Suspense>
  );
}
