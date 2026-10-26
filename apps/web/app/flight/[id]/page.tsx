import { Suspense } from "react";

import { FlightView } from "@/components/flight/flight-view";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Flight recording" };

export default function FlightPage() {
  return (
    <Suspense fallback={<LoadingBlock rows={8} label="Loading recording" />}>
      <FlightView />
    </Suspense>
  );
}
