"use client";

export function AutopilotNav() {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="text-xl font-semibold">Autopilot</h1>
        <p className="rounded-md border bg-primary/10 px-2 py-0.5 text-xs text-primary">run → observe → diagnose → propose → evaluate → approve → deploy → monitor</p>
      </div>
    </div>
  );
}
