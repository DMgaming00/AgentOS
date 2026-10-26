import { Suspense } from "react";

import { SecurityCenter } from "@/components/security/security-center";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Security Center" };

export default function SecurityPage() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <SecurityCenter />
    </Suspense>
  );
}
