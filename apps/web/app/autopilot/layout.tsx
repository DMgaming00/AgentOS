import { Suspense } from "react";

import { AutopilotNav } from "@/components/autopilot/nav";

export const metadata = { title: { default: "Autopilot", template: "%s · Autopilot · AgentOS" } };

export default function AutopilotLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-4">
      <Suspense fallback={<div className="h-10" />}>
        <AutopilotNav />
      </Suspense>
      {children}
    </div>
  );
}
