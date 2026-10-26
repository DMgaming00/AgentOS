import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("start workflow, watch DAG and events live, approve, download, survive refresh", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: "Workflows" }).click();
  await page.getByTestId("workflow-research-report").click();
  await expect(page.getByTestId("dag-graph")).toBeVisible();
  await page.getByTestId("start-run").click();
  await page.getByTestId("confirm-start-run").click();

  await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+/);
  const runUrl = page.url();
  // DAG with live step states
  for (const key of ["research_nimbus", "research_helix", "research_vectorforge", "analyze", "write_report"]) {
    await expect(page.getByTestId(`dag-node-${key}`)).toBeVisible();
  }
  await expect(page.getByTestId("stream-state")).toHaveAttribute("data-state", /open|closed/);

  // The writer pauses for approval; events arrive over SSE.
  await expect(page.getByTestId("run-status")).toContainText("Waiting approval", { timeout: 60_000 });
  await expect(page.getByTestId("dag-node-write_report")).toHaveAttribute("data-status", "waiting_approval");
  await expect(page.getByTestId("event-feed").locator('[data-event-type="approval.requested"]')).toHaveCount(1);
  await expect(page.getByTestId("dag-node-analyze")).toHaveAttribute("data-status", "succeeded");

  // Inspect a node: side panel with attempts and tool calls.
  await page.getByTestId("dag-node-research_helix").click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("tab", { name: /Tool calls/ }).click();
  await expect(page.getByTestId("tool-execution").first()).toContainText("document_search");
  await page.getByRole("button", { name: "Close panel" }).click();

  // Approve from the run page.
  const card = page.getByTestId("approval-card").first();
  await expect(card).toContainText("Writer Agent");
  await card.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByTestId("run-status")).toContainText("Succeeded", { timeout: 60_000 });

  // Download the artifact.
  const downloadPromise = page.waitForEvent("download");
  await page.getByTestId("download-inference-platform-comparison.md").click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("inference-platform-comparison.md");
  const fs = await import("node:fs/promises");
  const body = await fs.readFile((await download.path())!, "utf8");
  expect(body).toContain("## Numerical comparison");
  expect(body).toContain("## Conclusions (interpretation)");

  // Refresh: state is reloaded from the server, nothing is lost.
  const eventsBefore = await page.getByTestId("event-feed").locator("li").count();
  await page.reload();
  await expect(page.getByTestId("run-status")).toContainText("Succeeded");
  await expect(page.getByTestId("dag-node-write_report")).toHaveAttribute("data-status", "succeeded");
  await expect(page.getByTestId("event-feed").locator("li")).toHaveCount(eventsBefore);
  expect(page.url()).toBe(runUrl);
});
