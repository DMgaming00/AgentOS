import { Suspense } from "react";

import { LoadingBlock } from "@/components/ui/feedback";
import { ExperimentDetail } from "./experiment-detail";

export const metadata = { title: "Experiment" };

export default function Page() {
  return <Suspense fallback={<LoadingBlock rows={8} />}><ExperimentDetail /></Suspense>;
}
