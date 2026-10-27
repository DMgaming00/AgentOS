"use client";

import {
  Activity,
  Bot,
  Brain,
  CheckSquare,
  Clapperboard,
  Coins,
  Cpu,
  FileBox,
  FlaskConical,
  Flame,
  GitBranch,
  LayoutDashboard,
  ListChecks,
  LogOut,
  Menu,
  Moon,
  PlayCircle,
  Rocket,
  Settings,
  ShieldAlert,
  Sparkles,
  Stethoscope,
  Sun,
  Swords,
  Wand2,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState, useSyncExternalStore } from "react";

import { useAuth } from "@/lib/auth";
import { useApprovals, useSystem } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { Badge } from "./ui/badge";

const themeListeners = new Set<() => void>();
function subscribeTheme(l: () => void) {
  themeListeners.add(l);
  return () => void themeListeners.delete(l);
}

type NavItem = { href: string; label: string; icon: typeof Bot; exact?: boolean };
/** Grouped navigation. Every entry leads to a page backed by real data; there are no placeholder links. */
const NAV_GROUPS: { title: string; items: NavItem[] }[] = [
  {
    title: "Core",
    items: [
      { href: "/", label: "Overview", icon: LayoutDashboard, exact: true },
      { href: "/agents", label: "Agents", icon: Bot },
      { href: "/workflows", label: "Workflows", icon: GitBranch },
      { href: "/runs", label: "Runs", icon: PlayCircle },
      { href: "/approvals", label: "Approvals", icon: CheckSquare },
      { href: "/artifacts", label: "Artifacts", icon: FileBox },
    ],
  },
  {
    title: "Autopilot",
    items: [
      { href: "/autopilot", label: "Autopilot", icon: Sparkles, exact: true },
      { href: "/autopilot/failures", label: "Failure Intelligence", icon: Stethoscope },
      { href: "/autopilot/improvements", label: "Optimization Lab", icon: Wand2 },
      { href: "/autopilot/evaluations", label: "Evaluations", icon: ListChecks },
      { href: "/autopilot/experiments", label: "Experiments", icon: FlaskConical },
      { href: "/autopilot/deployments", label: "Deployments", icon: Rocket },
      { href: "/releases", label: "Release Center", icon: Rocket },
    ],
  },
  {
    title: "Engineering Labs",
    items: [
      { href: "/flight", label: "Flight Recorder", icon: Clapperboard },
      { href: "/chaos", label: "Chaos Lab", icon: Flame },
      { href: "/arena", label: "Agent Arena", icon: Swords },
      { href: "/memory", label: "Memory Lab", icon: Brain },
      { href: "/security", label: "Security Center", icon: ShieldAlert },
    ],
  },
  {
    title: "Operations",
    items: [
      { href: "/observability", label: "Observability", icon: Activity },
      { href: "/finops", label: "FinOps", icon: Coins },
      { href: "/workers", label: "Workers", icon: Cpu },
      { href: "/settings", label: "Settings", icon: Settings },
    ],
  },
];

function isActive(pathname: string, item: NavItem) {
  return item.exact ? pathname === item.href : pathname.startsWith(item.href);
}

function NavLinks({ pathname, onNavigate }: { pathname: string; onNavigate?: () => void }) {
  const pending = useApprovals({ status: "pending", limit: 1 });
  return (
    <nav className="flex flex-1 flex-col gap-3 overflow-y-auto px-2 pb-2" aria-label="Main">
      {NAV_GROUPS.map((group) => (
        <div key={group.title} role="group" aria-label={group.title} className="flex flex-col gap-0.5">
          <p className="px-3 pb-0.5 pt-1 text-[10px] font-semibold uppercase tracking-wider text-white/40">{group.title}</p>
          {group.items.map((item) => {
            const active = isActive(pathname, item);
            const count = item.href === "/approvals" ? pending.data?.total ?? 0 : 0;
            const Icon = item.icon;
            return (
              <Link
                key={item.href}
                href={item.href}
                onClick={onNavigate}
                aria-current={active ? "page" : undefined}
                className={cn("flex items-center gap-2.5 rounded-md px-3 py-1.5 text-sm transition-colors hover:bg-white/5", active && "bg-white/10 text-white")}
              >
                <Icon className="size-4" aria-hidden />
                <span className="flex-1">{item.label}</span>
                {count > 0 ? (
                  <span className="rounded-full bg-warning px-1.5 text-[10px] font-semibold text-black" aria-label={`${count} pending`}>{count}</span>
                ) : null}
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

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
        <MobileNav pathname={pathname} />
        <ModeBanner />
        <main className="mx-auto w-full min-w-0 max-w-[1400px] flex-1 p-4 md:p-6">{children}</main>
      </div>
    </div>
  );
}

function Sidebar({ pathname }: { pathname: string }) {
  const { session, logout, setWorkspace, role } = useAuth();
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
      <NavLinks pathname={pathname} />
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

/** Below the md breakpoint the sidebar is replaced by a top bar with the same grouped navigation. */
function MobileNav({ pathname }: { pathname: string }) {
  const [open, setOpen] = useState(false);
  const { logout } = useAuth();
  return (
    <div className="md:hidden">
      <div className="flex items-center justify-between bg-sidebar px-4 py-3 text-sidebar-foreground">
        <span className="flex items-center gap-2 font-semibold text-white"><span className="grid size-6 place-items-center rounded bg-primary text-xs font-bold text-primary-foreground">A</span> AgentOS</span>
        <button aria-label={open ? "Close navigation" : "Open navigation"} aria-expanded={open} onClick={() => setOpen(!open)} className="rounded p-1 hover:bg-white/10" data-testid="mobile-menu">
          {open ? <X className="size-5" /> : <Menu className="size-5" />}
        </button>
      </div>
      {open ? (
        <div className="max-h-[75vh] overflow-y-auto bg-sidebar pb-3 text-sidebar-foreground" data-testid="mobile-nav">
          <NavLinks pathname={pathname} onNavigate={() => setOpen(false)} />
          <button onClick={logout} className="mx-4 mt-2 flex items-center gap-1 rounded px-2 py-1 text-xs hover:bg-white/5"><LogOut className="size-3.5" aria-hidden /> Sign out</button>
        </div>
      ) : null}
    </div>
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
