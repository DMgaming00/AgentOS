import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("run the security evaluation, read the matrix, tiles, cases and incidents", async ({ page }) => {
  test.setTimeout(240_000);
  await login(page);
  await page.getByRole("link", { name: "Security Center" }).click();
  await expect(page.getByRole("heading", { name: "Security Center" })).toBeVisible();
  await expect(page.getByText("A blocked call proves a policy operated")).toBeVisible();

  await page.getByTestId("run-security").click();
  await expect(page.getByTestId("security-running")).toBeVisible();
  await expect(page.getByTestId("security-tiles")).toBeVisible({ timeout: 200_000 });

  await expect(page.getByTestId("sec-policy")).toContainText("11/11");
  await expect(page.getByTestId("sec-enforcement")).toContainText("100%");
  await expect(page.getByTestId("sec-exercised")).toContainText("100%");
  await expect(page.getByTestId("sec-unsafe")).toContainText("0");
  await expect(page.getByTestId("sec-false-positives")).toContainText("0 of 8");
  await expect(page.getByTestId("sec-secrets")).toContainText("0/4");

  // 7 goals x 4 surfaces, all passing; attack cases carry evidence for developers
  const matrix = page.getByTestId("injection-matrix");
  await expect(matrix.locator('[data-verdict="pass"]')).toHaveCount(28);
  await expect(matrix.locator('[data-verdict="fail"]')).toHaveCount(0);
  await expect(page.getByTestId("no-findings")).toBeVisible();
  await page.getByLabel("Case kind").selectOption("injection");
  await expect(page.getByTestId("case-table").locator("tr")).toHaveCount(29);
  await page.getByTestId("case-table").getByRole("button", { name: "show" }).first().click();
  await expect(page.getByTestId("case-table")).toContainText("injected_attempts");
  await page.getByLabel("Case kind").selectOption("policy");
  await expect(page.getByTestId("case-table")).toContainText("Arguments modified after approval are not executed");
});

test("viewer sees the verdicts but cannot run the suite or read raw evidence", async ({ page }) => {
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.goto("/security");
  await expect(page.getByRole("heading", { name: "Security Center" })).toBeVisible();
  await expect(page.getByTestId("run-security")).toHaveCount(0);
  await expect(page.getByTestId("security-tiles")).toBeVisible({ timeout: 30_000 });
  await page.getByLabel("Case kind").selectOption("injection");
  await expect(page.getByTestId("case-table").getByRole("button", { name: "show" })).toHaveCount(0);
});
