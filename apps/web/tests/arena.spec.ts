import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("pick contenders, score selection, read the leaderboard, then score the test split once", async ({ page }) => {
  test.setTimeout(300_000);
  await login(page);
  await page.getByRole("link", { name: "Agent Arena" }).click();
  await expect(page.getByRole("heading", { name: "Agent Arena" })).toBeVisible();
  await expect(page.getByText("says nothing about how any real model would score")).toBeVisible();
  await expect(page.getByTestId("corpus-state")).toContainText("all datasets verified");
  await expect(page.getByTestId("arena-corpus")).toContainText("Grounded Q&A");
  await expect(page.getByTestId("leakage-note")).toContainText("Identical cases across splits: 0");

  for (const n of ["Arena · Bare", "Arena · Careful", "Arena · Ungrounded"]) {
    await page.getByTestId("contender-list").locator("label", { hasText: n }).filter({ hasNotText: "(2 steps)" }).locator("input").check();
  }
  await page.getByLabel("Arena run name").fill("e2e arena");
  await page.getByTestId("start-arena").click();
  await expect(page.getByTestId("arena-stage")).toContainText("Scoring development", { timeout: 20_000 });
  await expect(page.getByTestId("arena-stage")).toContainText("test split locked", { timeout: 240_000 });
  await expect(page.getByTestId("test-locked")).toBeVisible();
  await expect(page.getByTestId("tab-test")).toBeDisabled();

  await page.getByTestId("tab-validation").click();
  const board = page.getByTestId("leaderboard-validation");
  await expect(board.locator("tbody tr")).toHaveCount(3);
  await expect(board.locator("tbody tr").first()).toContainText("Arena · Careful");
  await expect(board.locator('[data-testid="rank"]').first()).toHaveText("1");
  await board.getByRole("button", { name: "Arena · Bare" }).click();
  await expect(page.getByTestId("arena-failures")).toContainText("hidden by design");

  await page.getByTestId("final-test").click();
  await expect(page.getByTestId("arena-stage")).toContainText("Complete", { timeout: 240_000 });
  await expect(page.getByTestId("final-test")).toHaveCount(0);
  await page.getByTestId("tab-test").click();
  await expect(page.getByTestId("leaderboard-test").locator("tbody tr")).toHaveCount(3);
  await expect(page.getByTestId("selection-vs-test")).toBeVisible();
});

test("viewer can read the corpus but cannot start a run", async ({ page }) => {
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.goto("/arena");
  await expect(page.getByRole("heading", { name: "Agent Arena" })).toBeVisible();
  await expect(page.getByTestId("arena-corpus")).toBeVisible();
  await expect(page.getByTestId("start-arena")).toHaveCount(0);
});
