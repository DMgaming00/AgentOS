// Capture pages showing real Claude Haiku runs (needs live mode; run after scripts/demo.py research and
// scripts/real_haiku_autopilot.py). Output names are prefixed `haiku-`.
import { chromium } from "@playwright/test";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const out = process.argv[2] ?? "../../docs/screenshots";
const j = { "content-type": "application/json" };
const tok = await (await fetch(`${API}/api/v1/auth/login`, { method: "POST", headers: j, body: JSON.stringify({ email: "admin@agentos.dev", password: "agentos-admin" }) })).json();
const H = { Authorization: `Bearer ${tok.access_token}` };
const get = async (p) => (await fetch(`${API}${p}`, { headers: H })).json();
const runs = (await get("/api/v1/runs?limit=100")).items;
const research = runs.find((r) => r.status === "succeeded" && /research/i.test(r.workflow_name ?? r.name ?? ""));
const exps = (await get("/api/v1/experiments?limit=20")).items ?? [];
const exp = exps[0];
if (!research) throw new Error("no succeeded research run found");

const shots = [
  ["haiku-run-graph", `/runs/${research.id}`],
  ["haiku-run-cost", `/runs/${research.id}?view=cost`],
  ["haiku-run-trace", `/runs/${research.id}?view=trace`],
  ["haiku-flight-playback", `/flight/${research.id}`],
  ["haiku-autopilot-failures", "/autopilot/failures"],
  ...(exp ? [["haiku-autopilot-experiment", `/autopilot/experiments/${exp.id}`]] : []),
  ["haiku-finops", "/finops"],
  ["haiku-settings-live", "/settings"],
  ["haiku-artifacts", "/artifacts"],
];
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Email").fill("admin@agentos.dev");
await page.getByLabel("Password").fill("agentos-admin");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();
for (const [name, path] of shots) {
  if (process.env.ONLY && process.env.ONLY !== name) continue;
  await page.goto(`${BASE}${path}`);
  await page.waitForTimeout(3500);
  await page.screenshot({ path: `${out}/${name}.png` });
  console.log("captured", name);
}
await browser.close();
console.log("research run", research.id, "experiment", exp?.id);
