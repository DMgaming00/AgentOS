import { expect, test } from "@playwright/test";

import { API, apiToken, login, startAdhocRun } from "./helpers";

const FLOW = { nodes: [{ key: "a", type: "agent_task", agent: "Research Agent", tools: ["document_search"], input: { platform: "Nimbus Inference" } }] };

test("usage analytics from real runs, an enforced budget, anomaly detection and honest suggestions", async ({ page, request }) => {
  test.setTimeout(180_000);
  const runId = await startAdhocRun(request, FLOW, "finops-e2e");
  const h = await apiToken(request);
  await expect.poll(async () => (await (await request.get(`${API}/api/v1/runs/${runId}`, { headers: h })).json()).status, { timeout: 90_000 }).toBe("succeeded");

  await login(page);
  await page.getByRole("link", { name: "FinOps", exact: true }).click();
  await expect(page.getByRole("heading", { name: "FinOps" })).toBeVisible();
  await expect(page.getByTestId("estimate-label")).toContainText("Estimated cost — simulated pricing");
  await expect(page.getByTestId("fin-total")).toContainText("$");
  await expect(page.getByTestId("agent-cost")).toContainText("Research Agent");

  await page.getByRole("tab", { name: "Budgets" }).click();
  await page.getByLabel("Budget limit USD").fill("100.00");
  await page.getByTestId("create-budget").click();
  await expect(page.getByTestId("budget-table")).toContainText("Workspace", { timeout: 15_000 });
  await expect(page.getByTestId("budget-table").locator('[data-state="ok"]')).toHaveCount(1);
  await page.getByTestId("budget-table").getByRole("button", { name: "Alert only" }).click();
  await expect(page.getByTestId("budget-table")).toContainText("alert");
  await page.getByTestId("budget-table").getByRole("button", { name: "Delete" }).click();
  await expect(page.getByText("No budgets")).toBeVisible({ timeout: 15_000 });

  await page.getByRole("tab", { name: "Anomalies" }).click();
  await page.getByTestId("detect-anomalies").click();
  await expect(page.getByTestId("detect-result")).toContainText("Detection found");

  await page.getByRole("tab", { name: "Suggestions" }).click();
  await expect(page.getByText("Nothing is applied automatically")).toBeVisible();
});

test("viewer can read spend but cannot manage budgets or run detection", async ({ page }) => {
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.goto("/finops");
  await expect(page.getByRole("heading", { name: "FinOps" })).toBeVisible();
  await expect(page.getByTestId("estimate-label")).toBeVisible();
  await page.getByRole("tab", { name: "Budgets" }).click();
  await expect(page.getByTestId("create-budget")).toHaveCount(0);
  await page.getByRole("tab", { name: "Anomalies" }).click();
  await expect(page.getByTestId("detect-anomalies")).toHaveCount(0);
});
