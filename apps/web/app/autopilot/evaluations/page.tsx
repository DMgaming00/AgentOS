import { Suspense } from "react";

import { LoadingBlock } from "@/components/ui/feedback";
import { Evaluations } from "./evaluations";

export const metadata = { title: "Evaluations" };

export default function Page() {
  return <Suspense fallback={<LoadingBlock rows={6} />}><Evaluations /></Suspense>;
}
