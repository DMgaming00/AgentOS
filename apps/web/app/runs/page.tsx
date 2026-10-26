import { Suspense } from "react";

import { LoadingBlock } from "@/components/ui/feedback";
import { RunsList } from "./runs-list";

export const metadata = { title: "Runs" };

export default function RunsPage() {
  return (
    <Suspense fallback={<LoadingBlock />}>
      <RunsList />
    </Suspense>
  );
}
