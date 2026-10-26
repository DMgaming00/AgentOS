import { Suspense } from "react";

import { LoadingBlock } from "@/components/ui/feedback";
import { AgentDetail } from "./agent-detail";

export const metadata = { title: "Agent" };

export default function AgentPage() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <AgentDetail />
    </Suspense>
  );
}
