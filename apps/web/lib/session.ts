import type { Membership, User } from "@/types";

export type Session = {
  token: string;
  expiresAt: number;
  user: User;
  memberships: Membership[];
  workspaceId: string | null;
};

const KEY = "agentos.session";
const listeners = new Set<() => void>();
let cachedRaw: string | null | undefined;
let cachedValue: Session | null = null;

function parse(raw: string | null): Session | null {
  if (!raw) return null;
  try {
    const s = JSON.parse(raw) as Session;
    return s.token && s.expiresAt > Date.now() ? s : null;
  } catch {
    return null;
  }
}

/** Current session (stable reference until it changes, as useSyncExternalStore requires). */
export function loadSession(): Session | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(KEY);
  if (raw !== cachedRaw) {
    cachedRaw = raw;
    cachedValue = parse(raw);
  }
  return cachedValue;
}

export function saveSession(s: Session | null) {
  if (typeof window === "undefined") return;
  if (s) window.localStorage.setItem(KEY, JSON.stringify(s));
  else window.localStorage.removeItem(KEY);
  listeners.forEach((l) => l());
}

export function subscribeSession(listener: () => void): () => void {
  listeners.add(listener);
  const onStorage = (e: StorageEvent) => e.key === KEY && listener();
  window.addEventListener("storage", onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener("storage", onStorage);
  };
}
