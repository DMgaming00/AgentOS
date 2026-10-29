// Capture the second round of real-Haiku/Sonnet pages (hard-ops optimization, naive-prompt security, chaos n=20, memory).
// Run after scripts/real_hard_eval.py, real_haiku_chaos.py and real_haiku_memory.py while their data is in the database.
import { chromium } from "@playwright/test";
import { readFileSync } from "node:fs";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const out = process.argv[2] ?? "../../docs/screenshots";
const R = "../../benchmarks/results/real_haiku";
const hard = JSON.parse(readFileSync(`${R}/hard_results.json`, "utf8"));
const chaos = JSON.parse(readFileSync(`${R}/chaos_n20.json`, "utf8")).experiments;
const val = hard.find((x) => x.kind === "paired" && x.split === "validation");
const test = hard.find((x) => x.kind === "paired" && x.split === "test");
const c = (l) => chaos.find((e) => e.label === l).experiment_id;
const shots = [
  ["haiku2-optimizer-validation", `/autopilot/experiments/${val.experiment_id}`, null],
  ["haiku2-optimizer-test", `/autopilot/experiments/${test.experiment_id}`, null],
  ["haiku2-failures-diagnosis", "/autopilot/failures", null],
  ["haiku2-security-naive", "/security", "security-tiles"],
  ["haiku2-chaos-brownout-off", `/chaos/${c("Haiku brownout, retries off")}`, "scorecard"],
  ["haiku2-chaos-brownout-on", `/chaos/${c("Haiku brownout, retries on")}`, "scorecard"],
  ["haiku2-finops-spend", "/finops", null],
];
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1500 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Email").fill("admin@agentos.dev");
await page.getByLabel("Password").fill("agentos-admin");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();
for (const [name, path, testid] of shots) {
  if (process.env.ONLY && process.env.ONLY !== name) continue;
  await page.goto(`${BASE}${path}`);
  if (testid) await page.getByTestId(testid).first().waitFor({ timeout: 15000 }).catch(() => console.log("testid not found:", testid));
  await page.waitForTimeout(3500);
  await page.screenshot({ path: `${out}/${name}.png` });
  console.log("captured", name);
}
await browser.close();
