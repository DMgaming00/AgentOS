import { Suspense } from "react";

import { ArenaPage } from "@/components/arena/arena-page";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Agent Arena" };

export default function Page() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <ArenaPage />
    </Suspense>
  );
}
