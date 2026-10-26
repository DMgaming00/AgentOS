"use client";

import {
  Activity,
  Bot,
  CheckSquare,
  Clapperboard,
  FileBox,
  GitBranch,
  LayoutDashboard,
  LogOut,
  Moon,
  PlayCircle,
  Settings,
  Sparkles,
  Sun,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useSyncExternalStore } from "react";

import { useAuth } from "@/lib/auth";
import { useApprovals, useSystem } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { Badge } from "./ui/badge";

const themeListeners = new Set<() => void>();
function subscribeTheme(l: () => void) {
  themeListeners.add(l);
  return () => void themeListeners.delete(l);
}

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/runs", label: "Runs", icon: PlayCircle },
  { href: "/workflows", label: "Workflows", icon: GitBranch },
  { href: "/agents", label: "Agents", icon: Bot },
  { href: "/approvals", label: "Approvals", icon: CheckSquare },
  { href: "/autopilot", label: "Autopilot", icon: Sparkles },
  { href: "/flight", label: "Flight Recorder", icon: Clapperboard },
  { href: "/artifacts", label: "Artifacts", icon: FileBox },
  { href: "/observability", label: "Observability", icon: Activity },
  { href: "/settings", label: "Settings", icon: Settings },
];

export function AppShell({ children }: { children: React.ReactNode }) {
  const { session, ready } = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const isLogin = pathname.startsWith("/login");

  useEffect(() => {
    if (ready && !session && !isLogin) router.replace(`/login?next=${encodeURIComponent(pathname)}`);
  }, [ready, session, isLogin, pathname, router]);

  if (isLogin) return <>{children}</>;
  if (!ready || !session) return <div className="min-h-screen bg-background" aria-busy="true" />;

  return (
    <div className="flex min-h-screen">
      <Sidebar pathname={pathname} />
      <div className="flex min-w-0 flex-1 flex-col">
        <ModeBanner />
        <main className="mx-auto w-full max-w-[1400px] flex-1 p-4 md:p-6">{children}</main>
      </div>
    </div>
  );
}

function Sidebar({ pathname }: { pathname: string }) {
  const { session, logout, setWorkspace, role } = useAuth();
  const pending = useApprovals({ status: "pending", limit: 1 });
  const dark = useSyncExternalStore(subscribeTheme, () => document.documentElement.classList.contains("dark"), () => true);
  const toggleTheme = () => {
    const next = !dark;
    document.documentElement.classList.toggle("dark", next);
    window.localStorage.setItem("agentos.theme", next ? "dark" : "light");
    themeListeners.forEach((l) => l());
  };

  return (
    <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col bg-sidebar text-sidebar-foreground md:flex">
      <div className="flex items-center gap-2 px-4 py-4">
        <div className="grid size-7 place-items-center rounded-md bg-primary text-xs font-bold text-primary-foreground">A</div>
        <span className="font-semibold tracking-tight text-white">AgentOS</span>
      </div>
      <nav className="flex flex-1 flex-col gap-0.5 px-2" aria-label="Main">
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          const count = href === "/approvals" ? pending.data?.total ?? 0 : 0;
          return (
            <Link
              key={href}
              href={href}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex items-center gap-2.5 rounded-md px-3 py-2 text-sm transition-colors hover:bg-white/5",
                active && "bg-white/10 text-white",
              )}
            >
              <Icon className="size-4" aria-hidden />
              <span className="flex-1">{label}</span>
              {count > 0 ? (
                <span className="rounded-full bg-warning px-1.5 text-[10px] font-semibold text-black" aria-label={`${count} pending`}>
                  {count}
                </span>
              ) : null}
            </Link>
          );
        })}
      </nav>
      <div className="flex flex-col gap-2 border-t border-white/10 p-3 text-xs">
        {session && session.memberships.length > 1 ? (
          <select
            aria-label="Workspace"
            className="rounded border border-white/10 bg-transparent px-2 py-1"
            value={session.workspaceId ?? ""}
            onChange={(e) => {
              setWorkspace(e.target.value);
              window.location.reload();
            }}
          >
            {session.memberships.map((m) => (
              <option key={m.workspace_id} value={m.workspace_id} className="text-black">
                {m.workspace_name}
              </option>
            ))}
          </select>
        ) : (
          <span className="truncate text-white/70">{session?.memberships[0]?.workspace_name}</span>
        )}
        <span className="truncate" title={session?.user.email}>
          {session?.user.email} <span className="text-white/50">({role})</span>
        </span>
        <div className="flex gap-1">
          <button onClick={toggleTheme} className="flex items-center gap-1 rounded px-2 py-1 hover:bg-white/5" aria-label="Toggle theme">
            {dark ? <Sun className="size-3.5" /> : <Moon className="size-3.5" />}
          </button>
          <button onClick={logout} className="flex flex-1 items-center gap-1 rounded px-2 py-1 hover:bg-white/5">
            <LogOut className="size-3.5" aria-hidden /> Sign out
          </button>
        </div>
      </div>
    </aside>
  );
}

/** Always show which execution mode is active so simulated results are never mistaken for real ones. */
function ModeBanner() {
  const { data } = useSystem();
  if (!data) return null;
  const real = Object.entries(data.providers).filter(([k, v]) => k !== "mock" && (v as { configured?: boolean }).configured);
  return (
    <div className="flex flex-wrap items-center gap-2 border-b bg-card px-4 py-2 text-xs md:px-6">
      <span className="text-muted-foreground">Environment</span>
      <Badge tone="neutral">{data.environment}</Badge>
      <span className="text-muted-foreground">Planner</span>
      <Badge tone={data.planner_provider === "mock" ? "warning" : "primary"}>
        {data.planner_provider === "mock" ? "mock (simulated)" : data.planner_provider}
      </Badge>
      <span className="text-muted-foreground">Execution</span>
      <Badge tone={data.execution.paid_requests_allowed ? "danger" : "success"} data-testid="execution-mode" title={(data.execution.blockers as string[]).join("; ")}>
        {data.execution.paid_requests_allowed ? "LIVE: paid providers enabled" : "Offline: paid providers blocked"}
      </Badge>
      <span className="text-muted-foreground">API keys present</span>
      {real.length ? (
        real.map(([k]) => (
          <Badge key={k} tone="neutral">
            {k}{data.execution.paid_requests_allowed ? "" : " (blocked)"}
          </Badge>
        ))
      ) : (
        <Badge tone="neutral">none</Badge>
      )}
      {!data.redis_available ? <Badge tone="warning">Redis unavailable: live updates via polling</Badge> : null}
    </div>
  );
}
