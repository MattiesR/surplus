import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";

const token = () => readFileSync("/tmp/surplus-e2e/config/api_token", "utf8").trim();

test("unauthenticated visitors see the sign-in hint", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("Not signed in")).toBeVisible();
});

test("dashboard shows gauges after reading limits", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.getByRole("button", { name: "Read limits now" }).click();
  await expect(page.getByRole("meter")).toHaveCount(2);
  await expect(page.getByRole("heading", { name: "5-hour window" })).toBeVisible();
  await expect(page.getByText(/Resets in/).first()).toBeVisible();
  await expect(page.getByText("probe", { exact: true })).toBeVisible();
});

test("manual calibration is keyboard-usable and validated", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  const input = page.getByLabel("% used");
  await input.fill("150");
  await expect(page.getByRole("button", { name: "Save" })).toBeDisabled();
  await input.fill("34");
  await input.press("Enter");
  await expect(input).toHaveValue("");
});

test("chart has a table view", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.getByRole("button", { name: "Read limits now" }).click();
  await page.getByRole("button", { name: "Table" }).click();
  await expect(page.getByRole("table")).toBeVisible();
});
