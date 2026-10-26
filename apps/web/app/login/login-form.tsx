"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Field, Input } from "@/components/ui/inputs";
import { useAuth } from "@/lib/auth";

const DEV_USERS = [
  ["admin@agentos.dev", "agentos-admin", "admin"],
  ["developer@agentos.dev", "agentos-dev", "developer"],
  ["viewer@agentos.dev", "agentos-viewer", "viewer"],
];

export function LoginForm() {
  const { login } = useAuth();
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("admin@agentos.dev");
  const [password, setPassword] = useState("agentos-admin");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      const next = params.get("next");
      router.replace(next && next.startsWith("/") && !next.startsWith("//") ? next : "/");
    } catch (err) {
      setError(err instanceof TypeError ? "Cannot reach the API. Is it running?" : err instanceof Error ? err.message : "Login failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="w-full max-w-sm p-6">
      <div className="mb-6 flex items-center gap-2">
        <div className="grid size-8 place-items-center rounded-md bg-primary font-bold text-primary-foreground">A</div>
        <div>
          <h1 className="text-lg font-semibold">AgentOS</h1>
          <p className="text-xs text-muted-foreground">Development sign-in</p>
        </div>
      </div>
      <form onSubmit={submit} className="flex flex-col gap-4">
        <Field label="Email" htmlFor="email">
          <Input id="email" type="email" autoComplete="username" value={email} onChange={(e) => setEmail(e.target.value)} required />
        </Field>
        <Field label="Password" htmlFor="password">
          <Input
            id="password"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
          />
        </Field>
        {error ? (
          <p role="alert" className="text-sm text-danger">
            {error}
          </p>
        ) : null}
        <Button type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
      <div className="mt-6 rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
        <p className="mb-2 font-medium text-foreground">Seeded development accounts</p>
        <ul className="flex flex-col gap-1">
          {DEV_USERS.map(([u, p, r]) => (
            <li key={u}>
              <button
                type="button"
                className="text-left underline-offset-2 hover:underline"
                onClick={() => {
                  setEmail(u);
                  setPassword(p);
                }}
              >
                {u}
              </button>{" "}
              <span className="opacity-70">/ {p} ({r})</span>
            </li>
          ))}
        </ul>
        <p className="mt-2">Development authentication only. See docs/security.md.</p>
      </div>
    </Card>
  );
}
