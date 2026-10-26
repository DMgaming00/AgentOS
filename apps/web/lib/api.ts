import createClient, { type Middleware } from "openapi-fetch";

import type { paths } from "@/types/api";
import { loadSession, saveSession } from "./session";

export const API_URL = (process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public details?: unknown) {
    super(message);
  }
}

export function authHeaders(): Record<string, string> {
  const s = loadSession();
  const h: Record<string, string> = {};
  if (s?.token) h.Authorization = `Bearer ${s.token}`;
  if (s?.workspaceId) h["X-Workspace-Id"] = s.workspaceId;
  return h;
}

const auth: Middleware = {
  onRequest({ request }) {
    for (const [k, v] of Object.entries(authHeaders())) request.headers.set(k, v);
    return request;
  },
  async onResponse({ response }) {
    if (response.status === 401 && typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
      saveSession(null);
      // Full reload on purpose: drops every cached query belonging to the old session.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.href = `/login?next=${encodeURIComponent(window.location.pathname)}`;
    }
    return response;
  },
};

export const api = createClient<paths>({ baseUrl: API_URL });
api.use(auth);

type ErrorEnvelope = { error?: { code?: string; message?: string; details?: unknown } };

/** Unwrap an openapi-fetch result, throwing ApiError with the server's error envelope. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.error !== undefined || !result.response.ok) {
    const env = (result.error ?? {}) as ErrorEnvelope;
    throw new ApiError(
      result.response.status,
      env.error?.code ?? "http_error",
      env.error?.message ?? `Request failed with status ${result.response.status}`,
      env.error?.details,
    );
  }
  return result.data as T;
}

/** Raw authenticated fetch for endpoints that are not JSON (downloads, previews). */
export async function rawFetch(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`${API_URL}${path}`, { ...init, headers: { ...authHeaders(), ...(init?.headers ?? {}) } });
  if (!res.ok) {
    let body: ErrorEnvelope = {};
    try {
      body = await res.json();
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, body.error?.code ?? "http_error", body.error?.message ?? res.statusText);
  }
  return res;
}

export async function downloadArtifact(id: string, filename: string) {
  return downloadPath(`/api/v1/artifacts/${id}/download`, filename);
}

export async function downloadPath(path: string, filename: string) {
  const res = await rawFetch(path);
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function idempotencyKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}
