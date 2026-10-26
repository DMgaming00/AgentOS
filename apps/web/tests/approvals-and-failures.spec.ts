import { expect, test } from "@playwright/test";

import { login, startAdhocRun } from "./helpers";

test("rejecting an export on the approvals page fails the run without writing anything", async ({ page, request }) => {
  const runId = await startAdhocRun(request, {
    nodes: [{ key: "export", type: "tool_task", tool: "artifact_writer", name: "Export note",
              arguments: { filename: "e2e-note.md", content: "should not be written", reason: "e2e" } }],
  }, "e2e-reject");
  await login(page);
  await page.getByRole("link", { name: /Approvals/ }).click();
  const card = page.locator(`[data-testid="approval-card"]`).filter({ hasText: runId.slice(0, 8) });
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect(card.getByTestId("approval-content")).toContainText("should not be written");
  await card.getByLabel("Decision note").fill("not needed");
  await card.getByRole("button", { name: "Reject" }).click();
  await expect(page.getByTestId("approval-history")).toContainText("not needed");

  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("run-status")).toContainText("Failed", { timeout: 30_000 });
  await expect(page.getByText("No artifacts yet.")).toBeVisible();
  await expect(page.getByText("approval_rejected").first()).toBeVisible();
});

test("a failed run shows its error, retries and attempt history", async ({ page, request }) => {
  const runId = await startAdhocRun(request, {
    nodes: [
      { key: "flaky", type: "aggregation", name: "Flaky step",
        retry: { max_attempts: 2, backoff_base_seconds: 0.2, backoff_max_seconds: 0.5 },
        inject: { fail_on_attempts: [1, 2] } },
      { key: "after", type: "aggregation", depends_on: ["flaky"] },
    ],
  }, "e2e-fail");
  await login(page);
  await page.goto(`/runs/${runId}`);
  await expect(page.getByTestId("run-status")).toContainText("Failed", { timeout: 30_000 });
  await expect(page.getByText("Run error")).toBeVisible();
  await expect(page.getByTestId("dag-node-after")).toHaveAttribute("data-status", "skipped");
  await page.getByTestId("dag-node-flaky").click();
  await page.getByRole("tab", { name: /Attempts/ }).click();
  await expect(page.getByTestId("attempt-row")).toHaveCount(2);
  await expect(page.getByTestId("attempt-row").first()).toContainText("provider_unavailable");
  await expect(page.getByTestId("event-feed").locator('[data-event-type="step.retrying"]')).toHaveCount(1);
});

test("viewer can inspect but not approve", async ({ page, request }) => {
  await startAdhocRun(request, { nodes: [{ key: "gate", type: "approval_gate", action: "Viewer e2e gate" }] }, "e2e-viewer");
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.getByRole("link", { name: /Approvals/ }).click();
  const card = page.getByTestId("approval-card").filter({ hasText: "Viewer e2e gate" }).first();
  await expect(card).toBeVisible({ timeout: 30_000 });
  await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(card).toContainText("Viewers cannot decide approvals.");
});
