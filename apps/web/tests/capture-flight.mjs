// Capture Flight Recorder screenshots from a live stack: runs the research workflow, replays it, compares.
import { chromium } from "@playwright/test";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const out = process.argv[2] ?? "../../docs/screenshots";
const j = { "content-type": "application/json" };
const tok = await (await fetch(`${API}/api/v1/auth/login`, { method: "POST", headers: j, body: JSON.stringify({ email: "admin@agentos.dev", password: "agentos-admin" }) })).json();
const H = { Authorization: `Bearer ${tok.access_token}`, ...j };
const wfs = (await (await fetch(`${API}/api/v1/workflows`, { headers: H })).json()).items;
const wf = wfs.find((w) => w.name === "research-report");
const run = await (await fetch(`${API}/api/v1/workflows/${wf.id}/runs`, { method: "POST", headers: { ...H, "Idempotency-Key": `shot-${Date.now()}` }, body: "{}" })).json();
for (let i = 0; i < 120; i++) {
  const approvals = (await (await fetch(`${API}/api/v1/approvals?status=pending&run_id=${run.id}`, { headers: H })).json()).items ?? [];
  for (const a of approvals) await fetch(`${API}/api/v1/approvals/${a.id}/approve`, { method: "POST", headers: { ...H, "Idempotency-Key": `a-${a.id}` }, body: JSON.stringify({ reason: "screenshot" }) });
  const r = await (await fetch(`${API}/api/v1/runs/${run.id}`, { headers: H })).json();
  if (["succeeded", "failed"].includes(r.status)) break;
  await new Promise((res) => setTimeout(res, 500));
}
const replay = await (await fetch(`${API}/api/v1/runs/${run.id}/reexecute`, { method: "POST", headers: { ...H, "Idempotency-Key": `re-${run.id}` } })).json();
for (let i = 0; i < 120; i++) {
  const r = await (await fetch(`${API}/api/v1/runs/${replay.id}`, { headers: H })).json();
  if (["succeeded", "failed"].includes(r.status)) break;
  await new Promise((res) => setTimeout(res, 500));
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Email").fill("admin@agentos.dev");
await page.getByLabel("Password").fill("agentos-admin");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();
await page.goto(`${BASE}/flight`);
await page.waitForTimeout(2500);
await page.screenshot({ path: `${out}/flight-index.png` });
await page.goto(`${BASE}/flight/${run.id}`);
await page.getByTestId("flight-player").waitFor();
await page.getByTestId("frame-slider").fill("34");
await page.getByTestId("dag-node-write_report").click();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/flight-playback.png` });
await page.goto(`${BASE}/flight/compare?a=${run.id}&b=${replay.id}`);
await page.getByTestId("run-compare").waitFor();
await page.waitForTimeout(1500);
await page.screenshot({ path: `${out}/flight-compare.png` });
await browser.close();
console.log("captured", run.id, replay.id);
