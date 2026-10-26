// Capture Chaos Lab screenshots from a live stack: runs two seeded experiments with the same plan and compares them.
import { chromium } from "@playwright/test";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const out = process.argv[2] ?? "../../docs/screenshots";
const j = { "content-type": "application/json" };
const tok = await (await fetch(`${API}/api/v1/auth/login`, { method: "POST", headers: j, body: JSON.stringify({ email: "admin@agentos.dev", password: "agentos-admin" }) })).json();
const H = { Authorization: `Bearer ${tok.access_token}`, ...j };
const wf = (await (await fetch(`${API}/api/v1/workflows?limit=200`, { headers: H })).json()).items.find((w) => w.name === "research-report");
const ids = [];
for (const arm of ["recovery_policy no_retry (before)", "as_configured retries (after)"]) {
  const overrides = { trials: 10, max_affected_runs: 10, seed: 77, duration_seconds: 120, ...(arm.startsWith("recovery") ? {} : {}) };
  const body = { name: `Provider brownout: ${arm}`, workflow_id: wf.id, scenario: "provider-brownout", overrides };
  const r = await (await fetch(`${API}/api/v1/chaos/experiments`, { method: "POST", headers: { ...H, "Idempotency-Key": `cap-${arm}-${Date.now()}` }, body: JSON.stringify(body) })).json();
  ids.push(r.id);
  for (let i = 0; i < 240; i++) {
    const e = await (await fetch(`${API}/api/v1/chaos/experiments/${r.id}`, { headers: H })).json();
    if (e.status !== "running") break;
    await new Promise((res) => setTimeout(res, 500));
  }
}
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1500 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Email").fill("admin@agentos.dev");
await page.getByLabel("Password").fill("agentos-admin");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();
await page.goto(`${BASE}/chaos`);
await page.getByTestId("experiment-editor").waitFor();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/chaos-lab.png` });
await page.goto(`${BASE}/chaos/${ids[1]}`);
await page.getByTestId("scorecard").waitFor();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/chaos-scorecard.png`, fullPage: false });
await page.goto(`${BASE}/chaos/compare?a=${ids[0]}&b=${ids[1]}`);
await page.getByTestId("chaos-compare").waitFor();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/chaos-compare.png` });
await browser.close();
console.log("captured", ids.join(" "));
