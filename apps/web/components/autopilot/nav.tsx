"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

const TABS = [
  ["/autopilot", "Overview"],
  ["/autopilot/failures", "Failures"],
  ["/autopilot/improvements", "Improvements"],
  ["/autopilot/evaluations", "Evaluations"],
  ["/autopilot/experiments", "Experiments"],
  ["/autopilot/deployments", "Deployments"],
] as const;

export function AutopilotNav() {
  const path = usePathname();
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Autopilot</h1>
        <Badge tone="primary">run → observe → diagnose → propose → evaluate → approve → deploy → monitor</Badge>
      </div>
      <nav className="flex flex-wrap gap-1 border-b" aria-label="Autopilot sections">
        {TABS.map(([href, label]) => {
          const active = href === "/autopilot" ? path === href : path.startsWith(href);
          return (
            <Link key={href} href={href} aria-current={active ? "page" : undefined}
                  className={cn("-mb-px border-b-2 border-transparent px-3 py-2 text-sm text-muted-foreground hover:text-foreground",
                                active && "border-primary text-foreground")}>
              {label}
            </Link>
          );
        })}
      </nav>
    </div>
  );
}
