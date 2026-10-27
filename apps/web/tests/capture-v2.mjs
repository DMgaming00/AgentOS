// Capture AgentOS 2.0 pages (security, arena, memory, finops, releases, workers, run inspector) from a live stack.
// Run `make showcase` first so the pages have data.
import { chromium } from "@playwright/test";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const out = process.argv[2] ?? "../../docs/screenshots";
const j = { "content-type": "application/json" };
const tok = await (await fetch(`${API}/api/v1/auth/login`, { method: "POST", headers: j, body: JSON.stringify({ email: "admin@agentos.dev", password: "agentos-admin" }) })).json();
const H = { Authorization: `Bearer ${tok.access_token}` };
const runs = (await (await fetch(`${API}/api/v1/runs?limit=20`, { headers: H })).json()).items;
const done = runs.find((r) => r.status === "succeeded") ?? runs[0];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Email").fill("admin@agentos.dev");
await page.getByLabel("Password").fill("agentos-admin");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();
const pages = [
  ["security", "/security"],
  ["arena", "/arena"],
  ["memory-lab", "/memory"],
  ["finops", "/finops"],
  ["release-center", "/releases"],
  ["workers", "/workers"],
  ["run-inspector", `/runs/${done.id}?view=trace`],
];
for (const [name, path] of (process.env.ONLY ? pages.filter(([n]) => n === process.env.ONLY) : pages)) {
  await page.goto(`${BASE}${path}`);
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(1500);
  await page.screenshot({ path: `${out}/${name}.png` });
  console.log("captured", name);
}
await browser.close();
