import { type APIRequestContext, type Page, expect } from "@playwright/test";

export const API = process.env.E2E_API_URL ?? "http://localhost:8000";

export async function login(page: Page, email = "admin@agentos.dev", password = "agentos-admin") {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

export async function apiToken(request: APIRequestContext, email = "admin@agentos.dev", password = "agentos-admin") {
  const res = await request.post(`${API}/api/v1/auth/login`, { data: { email, password } });
  expect(res.ok()).toBeTruthy();
  return { Authorization: `Bearer ${(await res.json()).access_token}` };
}

/** Create a uniquely named workflow via the API and start a run. Returns the run id. */
export async function startAdhocRun(request: APIRequestContext, definition: object, name: string) {
  const h = await apiToken(request);
  const wf = await request.post(`${API}/api/v1/workflows`, { headers: h, data: { name: `${name}-${Date.now()}`, definition } });
  expect(wf.status(), await wf.text()).toBe(201);
  const run = await request.post(`${API}/api/v1/workflows/${(await wf.json()).id}/runs`, { headers: h, data: {} });
  expect(run.status()).toBe(201);
  return (await run.json()).id as string;
}
