import { Suspense } from "react";

import { MemoryLab } from "@/components/memory/memory-lab";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Memory Lab" };

export default function Page() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <MemoryLab />
    </Suspense>
  );
}
