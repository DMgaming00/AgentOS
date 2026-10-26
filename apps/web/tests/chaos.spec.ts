import { expect, test } from "@playwright/test";

import { API, apiToken, login } from "./helpers";

async function workflowId(request: Parameters<typeof apiToken>[0], name: string) {
  const h = await apiToken(request);
  const items = (await (await request.get(`${API}/api/v1/workflows?limit=200`, { headers: h })).json()).items as { id: string; name: string }[];
  return items.find((w) => w.name === name)!.id;
}

test("configure a seeded experiment, watch it finish, read the scorecard and the evidence", async ({ page }) => {
  test.setTimeout(150_000);
  await login(page);
  await page.getByRole("link", { name: "Chaos Lab" }).click();
  await expect(page.getByTestId("experiment-editor")).toBeVisible();

  // Editor: pick a scenario, then change the plan
  await page.getByLabel("Start from scenario").selectOption("provider-brownout");
  await expect(page.getByTestId("fault-row")).toHaveCount(2);
  await page.getByLabel("Name").fill("e2e brownout");
  await page.getByLabel("Target workflow").selectOption({ label: "research-report v1" });
  await page.getByLabel(/^Runs/).fill("4");
  await page.getByLabel("Max affected runs").fill("4");
  await page.getByLabel("Duration (s)").fill("90");
  await page.getByRole("button", { name: "Add fault" }).click();
  await expect(page.getByTestId("fault-row")).toHaveCount(3);
  await page.getByRole("button", { name: "Remove fault" }).last().click();
  await expect(page.getByTestId("fault-row")).toHaveCount(2);
  await page.getByTestId("start-experiment").click();

  await expect(page).toHaveURL(/\/chaos\/[0-9a-f-]+/);
  await expect(page.getByTestId("experiment-status")).toContainText(/Running|Succeeded/);
  // The research-report workflow waits for approval: the simulated reviewer decides, the experiment finishes.
  await expect(page.getByTestId("experiment-view")).toHaveAttribute("data-status", "completed", { timeout: 120_000 });
  await expect(page.getByTestId("scorecard")).toBeVisible();
  await expect(page.getByTestId("metric-completion")).toContainText("planned runs succeeded");
  await expect(page.getByTestId("metric-consistency")).toContainText("invariant checks passed");
  // Metric definitions and denominators are visible, not hidden
  await page.getByTestId("metric-recovery").getByText("Definition").click();
  await expect(page.getByTestId("metric-recovery")).toContainText("Denominator: affected runs");
  await expect(page.getByText("No combined score is computed")).toBeVisible();
  // Evidence: injection log and per-run links into the Flight Recorder
  await expect(page.getByTestId("injections")).toContainText(/provider_(timeout|rate_limit)/);
  await page.getByTestId("per-case").getByRole("link").first().click();
  await expect(page).toHaveURL(/\/flight\/[0-9a-f-]+/);
  await expect(page.getByTestId("flight-player")).toBeVisible();
});

test("compare two experiments run under the same fault schedule", async ({ page, request }) => {
  test.setTimeout(180_000);
  const h = await apiToken(request);
  const wf = await workflowId(request, "research-report");
  const body = { name: "e2e compare", workflow_id: wf, scenario: "provider-brownout", overrides: { trials: 10, max_affected_runs: 10, seed: 31, duration_seconds: 90 } };
  const ids: string[] = [];
  for (const arm of ["baseline", "candidate"]) {
    const r = await request.post(`${API}/api/v1/chaos/experiments`, { headers: { ...h, "Idempotency-Key": `e2e-${arm}-${Date.now()}` }, data: { ...body, name: `e2e ${arm}` } });
    expect(r.status(), await r.text()).toBe(201);
    ids.push((await r.json()).id);
    await expect.poll(async () => (await (await request.get(`${API}/api/v1/chaos/experiments/${ids.at(-1)}`, { headers: h })).json()).status, { timeout: 120_000 }).not.toBe("running");
  }
  await login(page);
  await page.goto(`/chaos/compare?a=${ids[0]}&b=${ids[1]}`);
  await expect(page.getByTestId("chaos-compare")).toBeVisible();
  await expect(page.getByTestId("paired-verdict")).toContainText("Paired completion");
  await expect(page.getByTestId("case-matrix").locator("span")).toHaveCount(10);
});

test("viewer can read experiments but cannot start one", async ({ page }) => {
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.goto("/chaos");
  await expect(page.getByRole("heading", { name: "Chaos Lab" })).toBeVisible();
  await expect(page.getByTestId("experiment-editor")).toHaveCount(0);
  await expect(page.getByText("simulated reviewer").first()).toBeVisible();
});
