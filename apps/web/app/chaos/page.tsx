import { Suspense } from "react";

import { ChaosHome } from "@/components/chaos/chaos-home";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Chaos Lab" };

export default function ChaosPage() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <ChaosHome />
    </Suspense>
  );
}
