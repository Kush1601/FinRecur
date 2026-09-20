import { test, expect } from "@playwright/test";

// Skipped unless E2E=1: the Playwright config starts both servers, while `make e2e`
// seeds Postgres before invoking the test.
test.skip(process.env.E2E !== "1", "set E2E=1 with the backend running to exercise this path");

test("Day 1 -> approve alias fix -> Day 2 recurrence", async ({ page }) => {
  await page.goto("/run");
  await expect(page.getByRole("heading", { name: "Run" })).toBeVisible();

  // The header deliberately exposes the same action. Scope this click to the
  // run workspace so the page owns the stream and renders its completion state.
  await page.locator("main").getByRole("button", { name: "Run day 1" }).click();

  await page.getByRole("link", { name: "Go to review queue" }).click();
  await expect(page.getByRole("heading", { name: "Needs your review" })).toBeVisible();

  await page
    .locator('a[href^="/review/clusters/"]')
    .filter({ hasText: "Receipts lack matching receivables" })
    .click();

  await expect(page.getByRole("heading", { name: "Cause" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Proposed fix" })).toBeVisible();
  await expect(page.getByText("add_counterparty_alias", { exact: true })).toBeVisible();
  await expect(page.getByText("Would match")).toBeVisible();

  await page.getByPlaceholder("Note").fill("Alias evidence checked against the source rows.");
  await page.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(page.getByText("approved", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Apply", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Apply", exact: true }).click();
  await expect(page.getByText(/Verified · prediction matched/)).toBeVisible();

  await page.goto("/run");
  await page.getByRole("button", { name: "Run again" }).click();
  // The completion card appears after the streamed decisions are grouped and
  // explained, not merely after the last SSE decision arrives.
  await expect(page.getByText(/^day2:/)).toBeVisible({ timeout: 120_000 });

  await page.goto("/activity");
  await page.getByRole("button", { name: "Recurrence" }).click();
  await expect(page.getByText(/alias/i).first()).toBeVisible();
  await expect(page.getByRole("main").getByText("day2", { exact: true })).toBeVisible();
});
