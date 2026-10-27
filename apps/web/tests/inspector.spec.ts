import { expect, test } from "@playwright/test";

import { API, apiToken, login, startAdhocRun } from "./helpers";

const FLOW = { nodes: [{ key: "a", type: "agent_task", agent: "Research Agent", tools: ["document_search"], input: { platform: "Nimbus Inference" } }] };

test("grouped navigation reaches every area", async ({ page }) => {
  await login(page);
  const nav = page.getByRole("navigation", { name: "Main" });
  for (const [group, links] of [
    ["Core", ["Overview", "Agents", "Workflows", "Runs", "Approvals", "Artifacts"]],
    ["Autopilot", ["Autopilot", "Failure Intelligence", "Optimization Lab", "Evaluations", "Experiments", "Deployments", "Release Center"]],
    ["Engineering Labs", ["Flight Recorder", "Chaos Lab", "Agent Arena", "Memory Lab", "Security Center"]],
    ["Operations", ["Observability", "FinOps", "Workers", "Settings"]],
  ] as const) {
    const g = nav.getByRole("group", { name: group });
    // the Approvals link carries a pending-count badge, so match the label as a prefix
    for (const l of links) await expect(g.getByRole("link", { name: new RegExp(`^${l}( \\d+ pending)?$`) })).toBeVisible();
  }
  await nav.getByRole("link", { name: "Failure Intelligence" }).click();
  await expect(page).toHaveURL(/\/autopilot\/failures/);
  await expect(nav.getByRole("link", { name: "Failure Intelligence" })).toHaveAttribute("aria-current", "page");
  await expect(nav.getByRole("link", { name: "Autopilot", exact: true })).not.toHaveAttribute("aria-current", "page");
  await nav.getByRole("link", { name: "Workers" }).click();
  await expect(page.getByRole("heading", { name: "Workers" })).toBeVisible();
  await expect(page.getByTestId("worker-table").locator('[data-healthy="true"]').first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("worker-tiles")).toContainText("Healthy workers");
});

test("agent run inspector: nine views of one run, one id, one trace", async ({ page, request }) => {
  test.setTimeout(120_000);
  const runId = await startAdhocRun(request, FLOW, "inspector-e2e");
  const h = await apiToken(request);
  await expect.poll(async () => (await (await request.get(`${API}/api/v1/runs/${runId}`, { headers: h })).json()).status, { timeout: 90_000 }).toBe("succeeded");
  const run = await (await request.get(`${API}/api/v1/runs/${runId}`, { headers: h })).json();

  await login(page);
  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("inspector-tabs")).toBeVisible();
  await expect(page.getByText("Execution graph")).toBeVisible();

  await page.getByTestId("view-flight").click();
  await expect(page).toHaveURL(new RegExp(`view=flight`));
  await expect(page.getByText(/Flight Recorder|Replay|recording/i).first()).toBeVisible();

  await page.getByTestId("view-trace").click();
  const trace = page.getByTestId("trace-tab");
  await expect(trace).toContainText(runId);
  expect(run.trace_id, "the run joined a trace when it was created").toMatch(/^[0-9a-f]{32}$/);
  await expect(page.getByTestId("trace-id")).toHaveText(run.trace_id);
  await expect(page.getByTestId("integrity")).toContainText("gap-free, monotonic");
  for (const kind of ["run", "step", "attempt", "model_call", "tool_call"]) await expect(page.locator(`[data-testid="waterfall"] [data-kind="${kind}"]`).first()).toBeVisible();

  await page.getByTestId("view-tools").click();
  await expect(page.getByTestId("tools-tab")).toContainText("document_search");

  await page.getByTestId("view-evaluation").click();
  await expect(page.getByText("Not an evaluation run")).toBeVisible();

  await page.getByTestId("view-cost").click();
  await expect(page.getByTestId("cost-tab")).toContainText("Estimated cost — simulated pricing");
  await expect(page.getByTestId("cost-total")).toContainText("$");

  await page.getByTestId("view-diagnosis").click();
  await expect(page.getByText("No failure signals for this run")).toBeVisible();

  await page.getByTestId("view-improvements").click();
  await page.getByTestId("view-deployment").click();
  await expect(page.getByTestId("deployment-tab")).toContainText("Research Agent");
  await expect(page.getByTestId("deployment-tab")).toContainText("production");

  // deep links survive a reload and the live graph is still one click away
  await page.reload();
  await expect(page.getByTestId("deployment-tab")).toBeVisible();
  await page.getByTestId("view-graph").click();
  await expect(page.getByText("Execution graph")).toBeVisible();
});

test("observability shows telemetry from stored records", async ({ page }) => {
  await login(page);
  await page.goto("/observability");
  const t = page.getByTestId("telemetry");
  await expect(t).toBeVisible({ timeout: 30_000 });
  await expect(t).toContainText("Workflow duration");
  await expect(t).toContainText("Approval wait");
  await expect(t).toContainText("Recovery duration");
  await expect(t).toContainText("live probe, not workload");
  await expect(page.getByTestId("dependency-graph")).toBeVisible();
  await expect(page.getByTestId("tool-latency")).toBeVisible();
  await expect(page.getByTestId("open-workers")).toBeVisible();
});

test("on a phone-sized screen the same grouped navigation opens from a menu", async ({ browser }) => {
  const ctx = await browser.newContext({ viewport: { width: 390, height: 800 } });
  const page = await ctx.newPage();
  await login(page);
  await expect(page.getByTestId("mobile-menu")).toBeVisible();
  await page.getByTestId("mobile-menu").click();
  const nav = page.getByTestId("mobile-nav");
  await expect(nav.getByRole("group", { name: "Engineering Labs" })).toBeVisible();
  await nav.getByRole("link", { name: "Memory Lab" }).click();
  await expect(page.getByRole("heading", { name: "Memory Lab" })).toBeVisible();
  await expect(page.getByTestId("mobile-nav")).toHaveCount(0);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
  expect(overflow, "no horizontal page scroll at 390px").toBe(false);
  await ctx.close();
});

test("no page scrolls horizontally on a phone-sized screen", async ({ browser }) => {
  test.setTimeout(180_000);
  const ctx = await browser.newContext({ viewport: { width: 390, height: 800 } });
  const page = await ctx.newPage();
  await login(page);
  const overflowing: string[] = [];
  for (const path of ["/", "/agents", "/workflows", "/runs", "/approvals", "/artifacts", "/autopilot", "/autopilot/failures", "/autopilot/improvements", "/autopilot/evaluations", "/autopilot/experiments", "/autopilot/deployments", "/releases", "/flight", "/chaos", "/arena", "/memory", "/security", "/observability", "/finops", "/workers", "/settings"]) {
    await page.goto(path);
    await page.locator("main h1").first().waitFor({ timeout: 15_000 });
    await page.waitForTimeout(600);
    if (await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1)) overflowing.push(path);
  }
  expect(overflowing, `pages with horizontal page scroll at 390px: ${overflowing.join(", ")}`).toEqual([]);
  await ctx.close();
});
