import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, newAccount } from "./support/hub";
import { apiOf } from "./support/memories";
import { enablePm, pmPath, seedPm } from "./support/pm";
import { enableTasks } from "./support/srs";
import { taskPath } from "./support/tasks";

/**
 * Criterion a7 of plan pm-daily-tracking in the browser, against the real API: a project that enabled the core plugin
 * pm has PM daily in its menu, the eight sections of one day with each task linking to its page and each blocker
 * naming who it waits on and its age; the day can be picked; a project without tasks, or without pm, says so; a reader
 * reads.
 */
test.skip(isDeployed, "enables plugins and seeds tasks through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("PM daily shows only where a project enabled it: no menu item and a 404 elsewhere, then the menu item opens the report", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const api = await apiOf(me);
  await page.goto(pmPath(project));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await page.goto(`/p/${project}`);
  await expect(page.getByTestId("nav-plugin-pm-daily")).toHaveCount(0);
  const off = await api.GET("/v1/x/pm/{project}/daily", { params: { path: { project } } });
  expect(off.response.status).toBe(404);

  await enableTasks(me, project);
  await enablePm(me, project);
  await page.reload();
  await page.getByTestId("nav-plugin-pm-daily").click();
  await expect(page).toHaveURL(pmPath(project));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("PM daily");
  await expect(main(page).getByTestId("pm-bare")).toBeVisible();
});

test("the report has its eight sections, a link to each task's page, and the blocker with who it waits on and its age", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await seedPm(me, project);
  await page.goto(pmPath(project));
  for (const id of ["done", "sprint", "late", "review", "stale", "blocked", "escalate", "load"]) {
    await expect(main(page).getByTestId(`pm-${id}`)).toBeVisible();
  }
  await expect(main(page).getByTestId("pm-sprint")).toContainText("Sprint 7");
  const blocked = main(page).getByTestId("pm-blocked-row");
  await expect(blocked).toHaveCount(1);
  await expect(blocked).toContainText("Acme bank");
  await expect(blocked).toContainText("Waiting for the gateway keys");
  await expect(blocked.getByTestId("pm-blocker-age")).toBeVisible();
  const review = main(page).getByTestId("pm-review-row");
  await expect(review).toHaveCount(1);
  await review.getByTestId("pm-task-link").click();
  await expect(page).toHaveURL(taskPath(project, "T-2"));
});

test("a day can be picked, and Today goes back", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await seedPm(me, project);
  await page.goto(pmPath(project));
  const date = main(page).getByTestId("pm-date");
  await expect(date).toHaveValue(/^\d{4}-\d{2}-\d{2}$/);
  await expect(main(page).getByTestId("pm-today")).toHaveCount(0);
  await date.fill("2026-01-07");
  await expect(page).toHaveURL(pmPath(project, "2026-01-07"));
  await expect(date).toHaveValue("2026-01-07");
  await expect(main(page).getByTestId("pm-done")).toBeVisible();
  await main(page).getByTestId("pm-today").click();
  await expect(page).toHaveURL(pmPath(project));
});

test("a reader reads the report; /x/pm leads to it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  const reader = newAccount("pm-reader");
  await new HubAdmin().grant(project, reader.login, "reader", "internal");
  await seedPm(me, project);
  await page.goto(`/p/${project}/x/pm`);
  await expect(page).toHaveURL(pmPath(project));
  await expect(main(page).getByTestId("pm-report")).toBeVisible();
});
