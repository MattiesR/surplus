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
  const card = page.getByRole("region", { name: "Manual calibration" });
  const input = card.getByLabel("% used");
  await input.fill("150");
  await expect(card.getByRole("button", { name: "Save" })).toBeDisabled();
  await input.fill("34");
  await input.press("Enter");
  await expect(input).toHaveValue("");
});

test("chart has a table view", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.getByRole("button", { name: "Read limits now" }).click();
  const card = page.getByRole("region", { name: /Weekly limit used this week/ });
  await card.getByRole("button", { name: "Table" }).click();
  await expect(card.getByRole("table")).toBeVisible();
});

test("pause and resume agent runs from the dashboard", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  const card = page.getByRole("region", { name: "Agent runs" });
  await card.getByLabel("Reason (optional)").fill("e2e demo");
  await card.getByRole("button", { name: "Pause 4 h" }).click();
  await expect(card.getByTestId("control-headline")).toContainText("Paused until");
  await expect(card.getByText("Reason: e2e demo")).toBeVisible();
  await card.getByRole("button", { name: "Resume" }).click();
  await expect(card.getByRole("button", { name: "Pause 1 h" })).toBeVisible();
});

test("plan timeline shows windows until the weekly reset, with a table view", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.getByRole("button", { name: "Read limits now" }).click();
  const card = page.getByRole("region", { name: /Plan until the weekly reset/ });
  await expect(card.getByTestId("plan-stats")).toContainText("Allowed now");
  await card.getByRole("button", { name: "Table" }).click();
  await expect(card.getByRole("cell", { name: "now", exact: true })).toBeVisible();
});

test("working hours are validated and saved", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  const card = page.getByRole("region", { name: "Your working hours" });
  const input = card.getByLabel(/When you usually use Claude/);
  await input.fill("whenever");
  await card.getByRole("button", { name: "Save" }).click();
  await expect(card.getByRole("alert")).toContainText("not a range");
  await input.fill("mon-fri 09:00-18:00");
  await card.getByRole("button", { name: "Save" }).click();
  await expect(card.getByText(/Template: mon-fri 09:00-18:00 \(45 h a week\)/)).toBeVisible();
  await card.getByRole("button", { name: "Remove" }).click();
  await expect(card.getByText(/No template/)).toBeVisible();
});

test("automatic runs are off by default and can be switched", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  const card = page.getByRole("region", { name: "Agent runs" });
  await expect(card.getByTestId("control-headline")).toContainText("Automatic runs are off");
  await card.getByRole("button", { name: "Turn automatic runs on" }).click();
  await expect(card.getByRole("button", { name: "Turn automatic runs off" })).toBeVisible();
  await card.getByRole("button", { name: "Turn automatic runs off" }).click();
  await expect(card.getByTestId("control-headline")).toContainText("Automatic runs are off");
});

test("the inbox is reachable from the nav and by shortcut", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /Inbox/ }).click();
  await expect(page.getByRole("heading", { name: "Inbox" })).toBeVisible();
  await expect(page.getByText("No open questions.")).toBeVisible();
  await page.keyboard.press("g");
  await page.keyboard.press("d");
  await expect(page).toHaveURL(/\/$/);
  await page.keyboard.press("g");
  await page.keyboard.press("i");
  await expect(page).toHaveURL(/\/inbox$/);
});
