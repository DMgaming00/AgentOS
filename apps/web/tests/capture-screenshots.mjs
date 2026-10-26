// Capture README screenshots from a live stack: node tests/capture-screenshots.mjs [outDir]
import { chromium } from "@playwright/test";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3000";
const API = process.env.E2E_API_URL ?? "http://localhost:8000";
const out = process.argv[2] ?? "../../docs/screenshots";

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 960 }, deviceScaleFactor: 1 });
const shot = (name) => page.screenshot({ path: `${out}/${name}.png`, fullPage: false });
const tok = await (await fetch(`${API}/api/v1/auth/login`, { method: "POST", headers: { "content-type": "application/json" },
  body: JSON.stringify({ email: "admin@agentos.dev", password: "agentos-admin" }) })).json();
const H = { Authorization: `Bearer ${tok.access_token}`, "content-type": "application/json" };
const wfs = await (await fetch(`${API}/api/v1/workflows`, { headers: H })).json();
const wf = wfs.items.find((w) => w.name === "research-report");
const run = await (await fetch(`${API}/api/v1/workflows/${wf.id}/runs`, { method: "POST", headers: H, body: "{}" })).json();

await page.goto(`${BASE}/login`);
await page.getByLabel("Email").fill("admin@agentos.dev");
await page.getByLabel("Password").fill("agentos-admin");
await page.getByRole("button", { name: "Sign in" }).click();
await page.getByRole("heading", { name: "Overview" }).waitFor();

await page.goto(`${BASE}/runs/${run.id}`);
await page.getByTestId("run-status").getByText("Waiting approval").waitFor({ timeout: 60000 });
await page.waitForTimeout(800);
await shot("run-waiting-approval");
await page.getByTestId("dag-node-analyze").click();
await page.waitForTimeout(600);
await shot("run-step-panel");
await page.getByRole("button", { name: "Close panel" }).click();
await page.goto(`${BASE}/approvals`);
await page.getByTestId("approval-card").first().waitFor();
await shot("approvals");
await page.getByTestId("approval-card").first().getByRole("button", { name: "Approve" }).click();
await page.goto(`${BASE}/runs/${run.id}`);
await page.getByTestId("run-status").getByText("Succeeded").waitFor({ timeout: 60000 });
await page.waitForTimeout(800);
await shot("run-succeeded");
await page.getByRole("button", { name: "Preview" }).first().click();
await page.getByTestId("artifact-preview").waitFor();
await shot("artifact-preview");
await page.keyboard.press("Escape");
for (const [path, name] of [["/", "overview"], ["/workflows", "workflows"], ["/agents", "agents"], ["/observability", "observability"], ["/settings", "settings"]]) {
  await page.goto(`${BASE}${path}`);
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(800);
  await shot(name);
}
await page.goto(`${BASE}/workflows/new`);
await page.getByRole("button", { name: "Generate plan" }).click();
await page.getByText("No issues. Execution order:").waitFor();
await page.waitForTimeout(600);
await shot("workflow-planner");
await browser.close();
console.log(`screenshots written to ${out}`);
