import { expect, test } from "@playwright/test";

import { API, apiToken, login, startAdhocRun } from "./helpers";

const WORKFLOW = {
  nodes: [
    { key: "research_a", type: "agent_task", agent: "Research Agent", tools: ["document_search"], input: { platform: "Nimbus Inference" }, retry: { max_attempts: 1 } },
    { key: "research_b", type: "agent_task", agent: "Research Agent", tools: ["document_search"], input: { platform: "Helix Serve" }, retry: { max_attempts: 1 } },
    { key: "analyze", type: "agent_task", agent: "Analysis Agent", depends_on: ["research_a", "research_b"], tools: ["calculator"], retry: { max_attempts: 1 } },
  ],
};

async function finishedRun(request: Parameters<typeof startAdhocRun>[0]) {
  const id = await startAdhocRun(request, WORKFLOW, "e2e-flight");
  const h = await apiToken(request);
  await expect
    .poll(async () => (await (await request.get(`${API}/api/v1/runs/${id}`, { headers: h })).json()).status, { timeout: 60_000 })
    .toMatch(/succeeded|failed/);
  return id;
}

test("play, step, scrub, inspect model calls, re-execute deterministically and compare", async ({ page, request }) => {
  test.setTimeout(150_000);
  const runId = await finishedRun(request);
  await login(page);
  await page.getByRole("link", { name: "Flight Recorder" }).click();
  await expect(page.getByTestId("flight-runs")).toBeVisible();
  await page.getByRole("link", { name: runId.slice(0, 8) }).first().click();

  // Recorded replay is labeled read-only and the event log is verified contiguous
  await expect(page.getByTestId("flight-mode")).toContainText("Recorded replay");
  await expect(page.getByTestId("flight-mode")).toContainText("Nothing is executed");
  await expect(page.getByText("event log contiguous")).toBeVisible();

  // Scrub to the start, then step forward frame by frame
  await page.getByTestId("frame-slider").fill("0");
  await expect(page.getByTestId("frame-counter")).toContainText("frame 0/");
  await expect(page.getByTestId("dag-node-research_a")).toHaveAttribute("data-status", "pending");
  await page.getByTestId("step-forward").click();
  await expect(page.getByTestId("frame-counter")).toContainText("frame 1/");
  await expect(page.getByTestId("current-frame")).toContainText("run.created");
  await page.getByTestId("step-back").click();
  await expect(page.getByTestId("frame-counter")).toContainText("frame 0/");

  // Play: node states move to succeeded as recorded frames are applied
  await page.getByTestId("play-pause").click();
  await expect(page.getByTestId("dag-node-analyze")).toHaveAttribute("data-status", "succeeded", { timeout: 30_000 });
  await expect(page.getByTestId("play-pause")).toHaveAttribute("aria-label", "Play"); // stopped at the last frame

  // Inspect a node's sanitized model calls
  await page.getByTestId("dag-node-research_a").click();
  const inspector = page.getByTestId("step-inspector");
  await expect(inspector).toContainText("research_a");
  await inspector.getByTestId("inspector-model-calls").getByRole("button").first().click();
  await expect(inspector).toContainText("Request");
  await expect(inspector.getByTestId("inspector-tools")).toContainText("document_search");

  // Deterministic re-execution creates a new, clearly labeled run fed from the recording
  await page.getByTestId("reexecute").click();
  await expect(page).toHaveURL(/\/flight\/[0-9a-f-]+/);
  await expect(page.getByTestId("flight-mode")).toContainText("Deterministic re-execution run", { timeout: 30_000 });
  await expect(page.getByTestId("flight-mode")).toContainText("side-effect-free");
  await page.getByRole("link", { name: "Compare with the original" }).click();
  await expect(page.getByTestId("run-compare")).toBeVisible();
  await expect(page.getByTestId("compare-summary")).toContainText("steps");
  await expect(page.getByTestId("compare-steps")).toContainText("analyze");
});

test("viewer can replay but cannot re-execute or export", async ({ page, request }) => {
  const runId = await finishedRun(request);
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.goto(`/flight/${runId}`);
  await expect(page.getByTestId("flight-player")).toBeVisible();
  await expect(page.getByTestId("reexecute")).toHaveCount(0);
  await expect(page.getByTestId("export-flight")).toHaveCount(0);
  await page.keyboard.press("End");
  await expect(page.getByTestId("dag-node-analyze")).toHaveAttribute("data-status", /succeeded|failed/);
});
