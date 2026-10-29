// Capture the real-Haiku Security, Arena, Chaos, Memory Lab and paired-experiment pages (run after the scripts in
// scripts/real_haiku_*.py, while live mode is still on so the banner is honest).
import { chromium } from "@playwright/test";
import { readFileSync } from "node:fs";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const out = process.argv[2] ?? "../../docs/screenshots";
const R = "../../benchmarks/results/real_haiku";
const chaos = JSON.parse(readFileSync(`${R}/chaos.json`, "utf8")).experiments;
const paired = JSON.parse(readFileSync(`${R}/paired.json`, "utf8"));
const byLabel = (l) => chaos.find((e) => e.label === l).experiment_id;
const shots = [
  ["haiku-security", "/security", "security-tiles"],
  ["haiku-arena", "/arena", null],
  ["haiku-chaos-brownout", `/chaos/${byLabel("Haiku brownout, retries off")}`, "scorecard"],
  ["haiku-chaos-crash", `/chaos/${byLabel("Haiku worker crashes, 5 attempts")}`, "scorecard"],
  ["haiku-memory", "/memory", null, "Experiments"],
  ["haiku-paired-experiment", `/autopilot/experiments/${paired.experiment_id}`, null],
];
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 1500 } });
await page.goto(`${BASE}/login`);
await page.getByLabel("Email").fill("admin@agentos.dev");
await page.getByLabel("Password").fill("agentos-admin");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();
for (const [name, path, testid, tab] of shots) {
  if (process.env.ONLY && process.env.ONLY !== name) continue;
  await page.goto(`${BASE}${path}`);
  if (testid) await page.getByTestId(testid).first().waitFor({ timeout: 15000 }).catch(() => console.log("testid not found:", testid));
  if (tab) await page.getByRole("tab", { name: tab }).click();
  await page.waitForTimeout(3500);
  await page.screenshot({ path: `${out}/${name}.png` });
  console.log("captured", name);
}
await browser.close();
