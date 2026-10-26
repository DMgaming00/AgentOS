// Capture Autopilot screenshots from a live stack (after running scripts/autopilot_demo.py all).
import { chromium } from "@playwright/test";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const out = process.argv[2] ?? "../../docs/screenshots";
const tok = await (await fetch(`${API}/api/v1/auth/login`, { method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ email: "admin@agentos.dev", password: "agentos-admin" }) })).json();
const H = { Authorization: `Bearer ${tok.access_token}` };
const exps = (await (await fetch(`${API}/api/v1/experiments`, { headers: H })).json()).items;
const good = exps.find((e) => e.decision === "eligible" && e.candidate_title?.startsWith("Return numeric"));
const fake = exps.find((e) => e.decision === "not_eligible");
const runs = (await (await fetch(`${API}/api/v1/evaluations/runs`, { headers: H })).json()).items;
const evalRun = runs.find((r) => r.arm === "baseline" && r.experiment_id === good?.id) ?? runs[0];

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Email").fill("admin@agentos.dev");
await page.getByLabel("Password").fill("agentos-admin");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();
const shots = [
  ["/autopilot", "autopilot-overview"],
  ["/autopilot/failures", "autopilot-failures"],
  [`/autopilot/improvements?agent=${good?.agent_id}`, "autopilot-improvements"],
  [`/autopilot/evaluations?run=${evalRun?.id}`, "autopilot-evaluations"],
  [`/autopilot/experiments/${good?.id}`, "autopilot-experiment-eligible"],
  [`/autopilot/experiments/${fake?.id}`, "autopilot-experiment-rejected"],
  ["/autopilot/deployments", "autopilot-deployments"],
];
for (const [path, name] of shots) {
  await page.goto(`${BASE}${path}`);
  await page.waitForLoadState("domcontentloaded");
  await page.waitForTimeout(2500);
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: false });
}
await page.goto(`${BASE}/autopilot/experiments/${good?.id}`);
await page.waitForTimeout(2500);
await page.getByTestId("gates").scrollIntoViewIfNeeded();
await page.screenshot({ path: `${out}/autopilot-gates.png`, fullPage: false });
await browser.close();
console.log("done");
