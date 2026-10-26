import { type APIRequestContext, expect, test } from "@playwright/test";

import { API, apiToken, login } from "./helpers";

const FLAWED =
  "You extract metrics from technical documents. Search the documents for the requested subject, then report each requested " +
  "value as written in the source, keeping prices in standard currency notation (for example $1.20). Cite the document section you used for every value.";

/** Setup through the API: a fresh flawed agent, a development-split evaluation, and failure analysis. */
async function flawedAgentWithFailures(request: APIRequestContext) {
  const h = await apiToken(request);
  const name = `E2E Extractor ${Date.now()}`;
  const agent = await (await request.post(`${API}/api/v1/agents`, { headers: h, data: {
    name, system_instructions: FLAWED, provider: "mock", model: "mock-extractor", model_configuration: { scenario: "extractor" },
    allowed_tool_ids: ["document_search", "structured_data_reader"], max_steps: 6, max_cost_usd: "0.5", timeout_seconds: 120 } })).json();
  const datasets = (await (await request.get(`${API}/api/v1/evaluations/datasets`, { headers: h })).json()).items;
  const dev = datasets.find((d: { split: string }) => d.split === "development");
  const run = await (await request.post(`${API}/api/v1/evaluations/run`, { headers: h, data: { agent_id: agent.id, dataset_id: dev.id, trials: 1, confirm_cost: false } })).json();
  await expect.poll(async () => (await (await request.get(`${API}/api/v1/evaluations/runs/${run.id}`, { headers: h })).json()).status,
                    { timeout: 60_000 }).toBe("completed");
  await request.post(`${API}/api/v1/autopilot/failures/analyze`, { headers: h, data: { agent_id: agent.id, window_hours: 2 } });
  return { agent, name };
}

test("diagnose, propose, experiment, approve, roll back - all from the Autopilot UI", async ({ page, request }) => {
  test.setTimeout(180_000);
  const { agent, name } = await flawedAgentWithFailures(request);
  await login(page);

  // Failures: diagnose the agent's schema-failure cluster
  await page.goto("/autopilot/failures");
  await expect(page.getByTestId("failure-clusters")).toContainText(name);
  const row = page.getByTestId("failure-clusters").locator("tr", { hasText: name }).filter({ hasText: "OUTPUT_SCHEMA_INVALID" });
  await row.getByRole("button", { name: "Diagnose" }).click();
  const detail = page.getByTestId("diagnosis-detail");
  await expect(detail).toContainText("currency values as strings", { timeout: 30_000 });
  await expect(detail).toContainText("not a verified root cause");
  await page.getByRole("button", { name: "Close panel" }).click();

  // Improvements: generate candidates, see the policy rejection, start an experiment on the fix
  await page.goto(`/autopilot/improvements?agent=${agent.id}`);
  await page.getByTestId("generate-candidates").click();
  await expect(page.getByTestId("candidate-card")).toHaveCount(3, { timeout: 30_000 });
  await expect(page.locator('[data-testid="candidate-card"][data-status="rejected_policy"]')).toContainText("adds tools");
  await expect(page.getByTestId("version-history")).toContainText("v2");
  const fix = page.getByTestId("candidate-card").filter({ hasText: "Return numeric values as JSON numbers" });
  await fix.getByTestId("run-experiment").click();
  await page.getByTestId("confirm-experiment").click();

  // Experiment: wait for the decision, check evidence and charts
  await expect(page).toHaveURL(/\/autopilot\/experiments\/[0-9a-f-]+/);
  await expect(page.getByTestId("experiment-decision")).toContainText("eligible", { timeout: 90_000 });
  await expect(page.getByTestId("experiment-summary")).toContainText("improvement demonstrated");
  await expect(page.getByTestId("ci-chart")).toBeVisible();
  await expect(page.getByTestId("gates")).toContainText("per_case_regressions");
  await expect(page.getByTestId("config-diff")).toContainText("system_instructions");
  await expect(page.getByTestId("per-case").locator('tr[data-regression="true"]')).toHaveCount(0);
  await page.getByTestId("request-promotion").click();
  await expect(page.getByText("An administrator must approve it")).toBeVisible();

  // Deployments: admin approves; the agent's active version changes
  await page.goto("/autopilot/deployments");
  const req = page.getByTestId("deploy-request").filter({ hasText: name });
  await req.getByTestId("approve-deploy").click();
  await expect(page.getByTestId("deployment-history").locator("tr", { hasText: name }).first()).toContainText("promotion");
  const h = await apiToken(request);
  await expect.poll(async () => {
    const a = await (await request.get(`${API}/api/v1/agents/${agent.id}`, { headers: h })).json();
    return a.active_version_id !== agent.active_version_id;
  }).toBe(true);

  // Rollback with a reason
  const rb = page.getByTestId(`rollback-${name}`);
  await rb.getByPlaceholder("Reason (required)").fill("e2e rollback check");
  await rb.getByTestId("rollback-button").click();
  await expect(page.getByTestId("deployment-history").locator("tr", { hasText: name }).first()).toContainText("rollback");
  await expect.poll(async () => (await (await request.get(`${API}/api/v1/agents/${agent.id}`, { headers: h })).json()).active_version_id)
    .toBe(agent.active_version_id);

  // Overview reflects persisted records and live activity
  await page.goto("/autopilot");
  await expect(page.getByTestId("autopilot-stats")).toBeVisible();
  await expect(page.getByTestId("autopilot-events").locator('[data-event-type="version.rolled_back"]').first()).toBeVisible();
});

test("viewer can inspect Autopilot but not change anything", async ({ page }) => {
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.goto("/autopilot/failures");
  await expect(page.getByTestId("execution-mode")).toContainText("Offline: paid providers blocked");
  await expect(page.getByRole("heading", { name: "Failure Intelligence" })).toBeVisible();
  await expect(page.getByTestId("analyze-failures")).toHaveCount(0);
  await page.goto("/autopilot/evaluations");
  await expect(page.getByTestId("datasets")).toContainText("validation");
  await expect(page.getByTestId("start-evaluation")).toHaveCount(0);
  await page.goto("/autopilot/deployments");
  await expect(page.getByText("Only administrators can roll back.")).toBeVisible();
});
