import { Suspense } from "react";

import { FlightIndex } from "@/components/flight/flight-index";
import { LoadingBlock } from "@/components/ui/feedback";

export const metadata = { title: "Flight Recorder" };

export default function FlightIndexPage() {
  return (
    <Suspense fallback={<LoadingBlock rows={6} />}>
      <FlightIndex />
    </Suspense>
  );
}
