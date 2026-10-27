import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("store, version and search memory; refuse secrets; run the strategy experiment and read measured results", async ({ page }) => {
  test.setTimeout(240_000);
  await login(page);
  await page.getByRole("link", { name: "Memory Lab" }).click();
  await expect(page.getByRole("heading", { name: "Memory Lab" })).toBeVisible();
  await expect(page.getByText("Retrieved memory is untrusted context")).toBeVisible();

  const key = `e2e.pref.${Date.now()}`;
  await page.getByLabel("Key", { exact: true }).fill(key);
  await page.getByLabel("Namespace", { exact: true }).fill("e2e");
  await page.getByLabel("Content", { exact: true }).fill("E2E customer prefers metric units");
  await page.getByTestId("create-record").click();
  await page.getByLabel("Namespace filter").fill("e2e");
  await page.getByTestId("memory-records").getByRole("button", { name: new RegExp(key) }).click();
  await page.getByLabel("Edit content").fill("E2E customer prefers imperial units");
  await page.getByTestId("update-record").click();
  await expect(page.getByTestId("record-history")).toContainText("update", { timeout: 15_000 });

  // credentials are refused
  await page.getByLabel("Key", { exact: true }).fill(`${key}.bad`);
  await page.getByLabel("Content", { exact: true }).fill("password: hunter2hunter2");
  await page.getByTestId("create-record").click();
  await expect(page.getByText("looks like a credential")).toBeVisible();

  await page.getByRole("tab", { name: "Retrieval" }).click();
  await page.getByLabel("Query").fill("customer prefers units");
  await page.getByLabel("Search namespace").fill("e2e");
  await page.getByTestId("run-search").click();
  await expect(page.getByTestId("search-results")).toContainText("imperial units");
  await expect(page.getByTestId("search-results")).toContainText("untrusted");

  await page.getByRole("tab", { name: "Audit log" }).click();
  await expect(page.getByTestId("memory-audit")).toContainText("denied");

  await page.getByRole("tab", { name: "Experiments" }).click();
  await page.getByTestId("run-experiment").click();
  await expect(page.getByTestId("strategy-table")).toBeVisible({ timeout: 180_000 });
  await expect(page.getByTestId("acc-none")).toContainText("/38");
  await expect(page.getByTestId("stale-summarized_stale")).toContainText("11");
  await expect(page.getByTestId("stale-summarized_refreshed")).toHaveText(/^0/);
  await expect(page.getByTestId("reuse-persistent_full")).not.toHaveText(/^0/);
  await expect(page.getByTestId("comparisons")).toContainText("Supported?");
});

test("viewer can read memory and results but cannot write or see the audit log", async ({ page }) => {
  await login(page, "viewer@agentos.dev", "agentos-viewer");
  await page.goto("/memory");
  await expect(page.getByRole("heading", { name: "Memory Lab" })).toBeVisible();
  await expect(page.getByTestId("create-record")).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "Audit log" })).toHaveCount(0);
  await page.getByRole("tab", { name: "Experiments" }).click();
  await expect(page.getByTestId("run-experiment")).toHaveCount(0);
});
