"use client";

import { createContext, useCallback, useContext, useMemo, useSyncExternalStore } from "react";

import { api, unwrap } from "./api";
import { type Session, loadSession, saveSession, subscribeSession } from "./session";

type AuthContextValue = {
  session: Session | null;
  ready: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  setWorkspace: (id: string) => void;
  role: string | null;
};

const noopSubscribe = () => () => {};

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const session = useSyncExternalStore(subscribeSession, loadSession, () => null);
  // false during server render / hydration, true once running in the browser
  const ready = useSyncExternalStore(noopSubscribe, () => true, () => false);

  const login = useCallback(async (email: string, password: string) => {
    const data = unwrap(await api.POST("/api/v1/auth/login", { body: { email, password } }));
    const s: Session = {
      token: data.access_token,
      expiresAt: Date.now() + data.expires_in * 1000,
      user: data.user,
      memberships: data.memberships,
      workspaceId: data.memberships[0]?.workspace_id ?? null,
    };
    saveSession(s);
  }, []);

  const logout = useCallback(() => {
    saveSession(null);
    // Full reload on purpose: drops every cached query belonging to the old session.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.href = "/login";
  }, []);

  const setWorkspace = useCallback((id: string) => {
    const prev = loadSession();
    if (prev) saveSession({ ...prev, workspaceId: id });
  }, []);

  const role = useMemo(
    () => session?.memberships.find((m) => m.workspace_id === session.workspaceId)?.role ?? null,
    [session],
  );

  const value = useMemo(() => ({ session, ready, login, logout, setWorkspace, role }), [session, ready, login, logout, setWorkspace, role]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}

export function canWrite(role: string | null): boolean {
  return role === "admin" || role === "developer";
}
