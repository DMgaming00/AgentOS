import { Suspense } from "react";

import { FinopsPage } from "@/components/finops/finops-page";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "FinOps" };

export default function Page() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <FinopsPage />
    </Suspense>
  );
}
