import { Suspense } from "react";

import { LoadingBlock } from "@/components/ui/feedback";
import { Improvements } from "./improvements";

export const metadata = { title: "Improvements" };

export default function Page() {
  return <Suspense fallback={<LoadingBlock rows={6} />}><Improvements /></Suspense>;
}
