"use client";

import { useParams } from "next/navigation";

import { FlightPlayer } from "@/components/flight/player";
import { ErrorState, LoadingBlock } from "@/components/ui/feedback";
import { useFlight } from "@/lib/flight";

export function FlightView() {
  const { id } = useParams<{ id: string }>();
  const q = useFlight(id);
  if (q.isLoading) return <LoadingBlock rows={8} label="Loading recording" />;
  if (q.error) return <ErrorState error={q.error} onRetry={() => void q.refetch()} />;
  if (!q.data) return null;
  return <FlightPlayer rec={q.data} key={q.data.run.id} />;
}
