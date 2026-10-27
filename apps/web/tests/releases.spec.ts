import { type APIRequestContext, expect, test } from "@playwright/test";

import { API, apiToken, login } from "./helpers";

const FLAWED =
  "You extract metrics from technical documents. Search the documents for the requested subject, then report each requested " +
  "value as written in the source, keeping prices in standard currency notation (for example $1.20). Cite the document section you used for every value.";

/** A flawed agent with failures, a rule-generated fix, and a completed eligible experiment: the real pipeline, through the API. */
async function eligibleExperiment(request: APIRequestContext) {
  const h = await apiToken(request);
  const name = `E2E Release ${Date.now()}`;
  const agent = await (await request.post(`${API}/api/v1/agents`, { headers: h, data: { name, system_instructions: FLAWED, provider: "mock", model: "mock-extractor", model_configuration: { scenario: "extractor" }, allowed_tool_ids: ["document_search"], max_steps: 6, max_cost_usd: "0.5", timeout_seconds: 120 } })).json();
  const datasets = (await (await request.get(`${API}/api/v1/evaluations/datasets?limit=200`, { headers: h })).json()).items;
  const pick = (split: string) => datasets.find((d: { split: string; name: string }) => d.split === split && d.name === "numeric-extraction");
  const run = await (await request.post(`${API}/api/v1/evaluations/run`, { headers: h, data: { agent_id: agent.id, dataset_id: pick("development").id, trials: 1, confirm_cost: false } })).json();
  await expect.poll(async () => (await (await request.get(`${API}/api/v1/evaluations/runs/${run.id}`, { headers: h })).json()).status, { timeout: 90_000 }).toBe("completed");
  const gen = await request.post(`${API}/api/v1/autopilot/candidates/generate`, { headers: h, data: { agent_id: agent.id, generator: "rules", window_days: 30 } });
  expect(gen.status(), await gen.text()).toBe(201);
  const [cand] = await gen.json();
  const exp = await (await request.post(`${API}/api/v1/experiments`, { headers: h, data: { agent_id: agent.id, candidate_id: cand.id, dataset_id: pick("validation").id, trials: 1, confirm_cost: false } })).json();
  await expect.poll(async () => (await (await request.get(`${API}/api/v1/experiments/${exp.id}`, { headers: h })).json()).status, { timeout: 120_000 }).toBe("completed");
  expect((await (await request.get(`${API}/api/v1/experiments/${exp.id}`, { headers: h })).json()).decision).toBe("eligible");
  return { agent, name, expId: exp.id as string };
}

test("gated staged release: gates, approval, canary traffic, advance to promotion", async ({ page, request }) => {
  test.setTimeout(420_000);
  const h = await apiToken(request);
  // a real, clean Security Center evaluation (the gate reads it)
  const sec = await request.post(`${API}/api/v1/security/runs`, { headers: h });
  expect([201, 409]).toContain(sec.status());
  await expect.poll(async () => (await (await request.get(`${API}/api/v1/security/overview`, { headers: h })).json()).latest?.status, { timeout: 200_000 }).toBe("completed");
  const { agent, name } = await eligibleExperiment(request);

  await login(page);
  await page.getByRole("link", { name: "Release Center" }).click();
  await expect(page.getByRole("heading", { name: "Release Center" })).toBeVisible();
  await expect(page.getByText("not a hosted production service")).toBeVisible();

  // gate preview, then create the production release
  await page.getByLabel("Release agent").selectOption({ label: name });
  await expect(page.getByTestId("gate-preview")).toContainText("security evaluation");
  await expect(page.locator('[data-gate="security_evaluation"]')).toHaveAttribute("data-passed", "true", { timeout: 20_000 });
  await expect(page.locator('[data-gate="human_approval"]')).toHaveAttribute("data-passed", "null");
  await page.getByLabel("Canary schedule").selectOption("fast");
  await page.getByLabel("Minimum samples per stage").fill("3");
  await page.getByTestId("create-release").click();
  await expect(page.getByTestId("release-status")).toContainText("pending approval", { timeout: 20_000 });
  await expect(page.getByTestId("stage-ladder")).toContainText("50%");

  // the existing approval flow (administrator) activates it
  const reqs = await (await request.get(`${API}/api/v1/deployments/requests?status=pending`, { headers: h })).json();
  const req = reqs.items.find((r: { agent_id: string }) => r.agent_id === agent.id);
  expect((await request.post(`${API}/api/v1/deployments/requests/${req.id}/approve`, { headers: h, data: { reason: "e2e" } })).status()).toBe(200);
  await expect(page.getByTestId("release-status")).toContainText("active", { timeout: 20_000 });
  await expect(page.getByTestId("canary-badge")).toContainText("50%");
  await expect(page.getByTestId("approval-audit")).toContainText("approved");

  // live traffic through both arms
  const wf = await (await request.post(`${API}/api/v1/workflows`, { headers: h, data: { name: `release-live-${Date.now()}`, definition: { nodes: [{ key: "extract", type: "agent_task", agent: name, prompt: "Extract p95 latency", input: { fields: [{ key: "nimbus_p95_latency_ms", label: "p95 latency", unit: "ms", subject: "Nimbus Inference" }], documents: ["platform-nimbus.md"] }, retry: { max_attempts: 1 }, output_contract: { type: "object", required: ["nimbus_p95_latency_ms"], properties: { nimbus_p95_latency_ms: { type: "number" } } } }], budget: { max_model_calls: 10, max_cost_usd: 0.5, max_concurrency: 1 } } } })).json();
  const runIds: string[] = [];
  for (let i = 0; i < 20; i++) runIds.push((await (await request.post(`${API}/api/v1/workflows/${wf.id}/runs`, { headers: h, data: {} })).json()).id);
  for (const id of runIds) await expect.poll(async () => (await (await request.get(`${API}/api/v1/runs/${id}`, { headers: h })).json()).status, { timeout: 90_000 }).toMatch(/succeeded|failed/);

  await page.reload();
  await expect(page.getByTestId("live-comparison")).toContainText("canary steps");
  await expect(page.locator('[data-rule="failure_rate_increase"]')).toBeVisible();
  await expect(page.locator('[data-gate="stage_samples"]')).toHaveAttribute("data-passed", "true", { timeout: 20_000 });
  await expect(page.locator('[data-gate="rollback_rules_clear"]')).toHaveAttribute("data-passed", "true");
  await page.getByTestId("advance-release").click();
  await expect(page.getByTestId("release-status")).toContainText("completed", { timeout: 20_000 });
  await expect(page.getByTestId("release-events")).toContainText("completed");
  await expect(page.getByTestId("canary-badge")).toHaveCount(0);
});

test("viewer can read releases but cannot create, advance or roll back", async ({ page }) => {
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.goto("/releases");
  await expect(page.getByRole("heading", { name: "Release Center" })).toBeVisible();
  await expect(page.getByTestId("environments")).toBeVisible();
  await expect(page.getByTestId("create-release")).toHaveCount(0);
  await expect(page.getByTestId("advance-release")).toHaveCount(0);
});
