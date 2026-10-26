import { Suspense } from "react";

import { CompareView } from "@/components/flight/compare-view";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Compare runs" };

export default function ComparePage() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <CompareView />
    </Suspense>
  );
}
