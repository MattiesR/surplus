import { expect, test } from "@playwright/test";
import { appendFileSync, existsSync, readFileSync } from "node:fs";

const E2E = "/tmp/surplus-e2e";
const token = () => readFileSync(`${E2E}/config/api_token`, "utf8").trim();

test.describe.configure({ mode: "serial" });

test("wizard creates a project with correct files on disk", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.goto("/projects/new");
  await page.getByLabel("Project name").fill("Chart Maker");
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("One paragraph").fill("Turns CSV files into charts.");
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByLabel("Goal 1 title").fill("Ship a CLI");
  await page.getByLabel("Goal 1 acceptance criteria").fill("prints help\nexits 0 on success");
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Next" }).click();
  await page.getByRole("button", { name: "Create project" }).click();

  await expect(page).toHaveURL(/\/projects\/chart-maker$/);
  await expect(page.getByRole("heading", { name: "Chart Maker", level: 1 })).toBeVisible();

  const dir = `${E2E}/projects/chart-maker`;
  expect(existsSync(`${dir}/.git`)).toBe(true);
  const project = readFileSync(`${dir}/.surplus/project.md`, "utf8");
  expect(project).toContain("id: chart-maker\nname: Chart Maker\n");
  expect(project).toContain("Turns CSV files into charts.");
  const goals = readFileSync(`${dir}/.surplus/goals.md`, "utf8");
  expect(goals).toContain("## G1. Ship a CLI\n\nAcceptance:\n- prints help\n- exits 0 on success\n");
});

test("backlog: add and complete a task from the UI", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.goto("/projects/chart-maker/backlog");
  await page.getByLabel("New task").fill("Parse CSV headers");
  await page.getByLabel("Acceptance (optional)").fill("headers become series names");
  await page.getByRole("button", { name: "Add", exact: true }).click();
  await expect(page.getByText("Parse CSV headers")).toBeVisible();
  await page.getByLabel("Mark T-001 done").click();
  await expect(page.getByLabel("Mark T-001 not done")).toBeChecked();
  const backlog = readFileSync(`${E2E}/projects/chart-maker/.surplus/backlog.md`, "utf8");
  expect(backlog).toContain("- [x] T-001 (P2) Parse CSV headers — acceptance: headers become series names\n");
});

test("an edit in an external editor shows up within 2 s", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.goto("/projects/chart-maker/backlog");
  await expect(page.getByText("Parse CSV headers")).toBeVisible();
  appendFileSync(`${E2E}/projects/chart-maker/.surplus/backlog.md`, "- [ ] T-002 (P1, ~S) Edited outside the UI\n");
  await expect(page.getByText("Edited outside the UI")).toBeVisible({ timeout: 2000 });
});

test("run panel runs a preflight and refuses to start when something is missing", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.goto("/projects/chart-maker/runs");
  await expect(page.getByText("No runs yet")).toBeVisible();
  await page.goto("/projects/chart-maker/backlog");
  await page.getByRole("button", { name: "Run", exact: true }).first().click();
  const panel = page.getByRole("group", { name: "Run T-002" });
  // The e2e config has no setup token, so preflight must list it as a problem.
  await expect(panel.getByRole("alert", { name: "Problems" })).toContainText("CLAUDE_CODE_OAUTH_TOKEN");
  await expect(panel.getByRole("button", { name: "Start run on T-002" })).toBeDisabled();
});

test("a broken backlog line is reported with its line number", async ({ page }) => {
  await page.goto(`/auth?token=${token()}`);
  await page.goto("/projects/chart-maker/backlog");
  appendFileSync(`${E2E}/projects/chart-maker/.surplus/backlog.md`, "- [ ] T-3 broken\n");
  await expect(page.getByRole("alert").filter({ hasText: "backlog.md" }).first()).toContainText("line");
});
