import { expect, test } from "@playwright/test";

test("monitor page: curve, gauges, sortable and filterable states, series card, nav", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/monitor");
  await expect(page.getByRole("region", { name: "yield curve" })).toBeVisible();
  await expect(page.getByText(/no inversions|inverted:/)).toBeVisible();
  await expect(page.getByText("Sahm rule (UNRATE)")).toBeVisible();
  await expect(page.getByText("Recession probability, 12 months ahead")).toBeVisible();
  const world = page.getByRole("region", { name: "international unemployment" });
  await expect(world.getByText("Germany")).toBeVisible();
  await world.getByRole("button", { name: /Japan/ }).click();
  await expect(page.getByRole("dialog", { name: /LRHUTTTTJPM156S series card/ })).toBeVisible();
  await page.keyboard.press("Escape");
  const table = page.getByRole("region", { name: "state unemployment" });
  const firstState = () => table.locator("tbody tr").first().locator("td").first().innerText();
  await table.getByRole("columnheader", { name: /^state/ }).click();
  expect(await firstState()).toMatch(/^AK /);                       // alphabetical ascending
  await table.getByLabel("filter states").fill("TX");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await table.locator("tbody tr").first().click();
  await expect(page.getByRole("dialog", { name: /TXUR series card/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.getByRole("link", { name: "Desk", exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  await expect(page.getByRole("link", { name: "Monitor", exact: true })).toHaveAttribute("href", "/monitor");
  expect(errors).toEqual([]);
});
