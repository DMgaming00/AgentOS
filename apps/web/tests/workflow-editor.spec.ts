import { expect, test } from "@playwright/test";

import { login } from "./helpers";

test("plan a workflow from natural language, inspect DAG and validation", async ({ page }) => {
  await login(page);
  await page.goto("/workflows/new");
  await page.getByRole("button", { name: "Generate plan" }).click();
  await expect(page.getByText(/mock\/mock-planner \(simulated\)/)).toBeVisible();
  await expect(page.getByTestId("dag-node-write_report")).toBeVisible();
  await expect(page.getByText("No issues. Execution order:")).toBeVisible();

  // Introduce a cycle: validation reports it and Save is disabled.
  const json = page.getByTestId("workflow-json");
  const def = JSON.parse(await json.inputValue());
  def.nodes[0].depends_on = ["write_report"];
  await json.fill(JSON.stringify(def, null, 2));
  await expect(page.getByTestId("validation-issues")).toContainText("cycle");
  await expect(page.getByRole("button", { name: "Save workflow" })).toBeDisabled();
});
