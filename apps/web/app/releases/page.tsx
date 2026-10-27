import { Suspense } from "react";

import { ReleaseCenter } from "@/components/release/release-center";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Release Center" };

export default function Page() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <ReleaseCenter />
    </Suspense>
  );
}
