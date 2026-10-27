import { Suspense } from "react";

import { WorkersPage } from "@/components/observability/workers-page";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Workers" };

export default function Page() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <WorkersPage />
    </Suspense>
  );
}
