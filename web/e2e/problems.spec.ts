import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { claimPath } from "./support/canvas";
import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, newAccount } from "./support/hub";
import { goalsPath, itemPath, RELEASED_PLAN } from "./support/loop";
import { open } from "./support/plans";
import { PROBLEM, problemsPath, putProblemByApi, seedProblems } from "./support/problems";
import { toast } from "./support/toast";

/**
 * The Problems pages of a project (plan problem-oriented-hub, criterion a7), against the real API: the list by status
 * and rank; a problem's page with what hurts, its metric from its key's signals, the goals, claims by canvas, plans and
 * Loop chains that go from it, and its history; a writer adds, edits, closes and reopens a problem; the plans list, a
 * plan's page, a goal's page, a claim's page and a chain show the problem as a chip that opens it. A reader reads and
 * writes nothing. axe checks the pages in light and dark.
 */
test.skip(isDeployed, "seeds the problems through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** axe on what the page shows, in the light theme and then in the dark one. */
async function bothThemes(page: Page, label: string) {
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme, reducedMotion: "reduce" });
    await expect(page.locator("html")).toHaveClass(new RegExp(scheme));
    await expectNoSeriousViolations(page, `${label} (${scheme})`);
  }
  await page.emulateMedia({ colorScheme: "light", reducedMotion: "reduce" });
}

test("a writer adds a problem, edits it, closes it as dropped and reopens it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedProblems(me, project);

  await open(page, problemsPath(project));
  await expect(main(page).getByRole("heading", { level: 1, name: "Problems" })).toBeVisible();
  await expect(page.getByTestId("nav-problems")).toHaveAttribute("aria-current", "page");
  const table = main(page).getByTestId("problems-table");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(table.getByTestId("problem-link")).toHaveText(PROBLEM.statement);
  await expect(table.getByTestId("problem-status")).toHaveText("Open");
  await expect(table).toContainText("checkout.conversion, mean over 30 days, for tenant lumen-shops");
  await bothThemes(page, "the problems list");

  // A new problem: the form says what it lacks, then the problem takes the next code and its page opens.
  await main(page).getByTestId("problems-new").click();
  const dialog = page.getByTestId("problem-new-dialog");
  await expect(dialog.getByRole("heading", { name: "New problem" })).toBeVisible();
  await bothThemes(page, "the new problem dialog");
  await dialog.getByTestId("problem-new-form-submit").click();
  await expect(dialog.getByTestId("problem-form-wrong")).toHaveText("4 fields need a change before you save.");
  await expect(dialog.getByTestId("problem-statement-input")).toHaveAttribute("aria-invalid", "true");
  await dialog.getByTestId("problem-statement-input").fill("Shops reorder the same stock by hand every week");
  await dialog.getByTestId("problem-who-input").fill("Owners of shops with a steady stock");
  await dialog.getByTestId("problem-when-input").fill("Every Monday morning");
  await dialog.getByTestId("problem-workaround-input").fill("They copy last week's order from a spreadsheet");
  await dialog.getByTestId("problem-key-input").selectOption("checkout.conversion");
  await dialog.getByTestId("problem-aggregate-input").selectOption("last");
  await dialog.getByTestId("problem-note-input").fill("from the October interviews");
  await dialog.getByTestId("problem-new-form-submit").click();
  await expect(toast(page, "P-2 added")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${problemsPath(project, "P-2")}$`));
  await expect(main(page).getByTestId("problem-code")).toHaveText("P-2");
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Shops reorder the same stock by hand every week");
  await expect(main(page).getByTestId("problem-head").getByTestId("problem-status")).toHaveText("Open");
  await expect(main(page).getByTestId("problem-who")).toHaveText("Owners of shops with a steady stock");
  await expect(main(page).getByTestId("problem-metric-text")).toHaveText("checkout.conversion, last over 90 days");
  await expect(main(page).getByTestId("problem-owner")).toHaveText(me.login);
  await expect(main(page).getByTestId("problem-rank")).toHaveText("2");

  // An edit is the next revision; the history says what it changed.
  await main(page).getByTestId("problem-edit").click();
  const edit = page.getByTestId("problem-edit-dialog");
  await edit.getByTestId("problem-workaround-input").fill("They copy last week's order and fix it by phone");
  await edit.getByTestId("problem-edit-form-submit").click();
  await expect(toast(page, "P-2 saved as revision 2")).toBeVisible();
  await expect(main(page).getByTestId("problem-workaround")).toHaveText("They copy last week's order and fix it by phone");
  await expect(main(page).getByTestId("problem-page")).toHaveAttribute("data-revision", "2");
  const history = main(page).getByTestId("problem-history");
  await expect(history.getByTestId("problem-revision-row")).toHaveCount(2);
  await expect(history.getByTestId("problem-revision-row").first()).toContainText("changed how they cope");

  // Closed as dropped, with why; then reopened.
  await main(page).getByTestId("problem-close").click();
  const close = page.getByTestId("problem-close-dialog");
  await close.getByTestId("problem-close-dropped").click();
  await close.getByTestId("problem-close-submit").click();
  await expect(close.getByTestId("problem-close-reason-input")).toHaveAttribute("aria-invalid", "true");
  await close.getByTestId("problem-close-reason-input").fill("The supplier's portal reorders on its own now");
  await expect(close.getByTestId("problem-close-submit")).toHaveText("Close as dropped");
  await close.getByTestId("problem-close-submit").click();
  await expect(toast(page, "P-2 is dropped")).toBeVisible();
  await expect(main(page).getByTestId("problem-head").getByTestId("problem-status")).toHaveText("Dropped");
  await expect(main(page).getByTestId("problem-closed")).toContainText("The supplier's portal reorders on its own now");
  await bothThemes(page, "a dropped problem");

  await main(page).getByTestId("problem-reopen").click();
  await page.getByTestId("problem-reopen-dialog").getByTestId("problem-reopen-submit").click();
  await expect(toast(page, "P-2 is open again")).toBeVisible();
  await expect(main(page).getByTestId("problem-head").getByTestId("problem-status")).toHaveText("Open");
  await expect(main(page).getByTestId("problem-closed")).toHaveCount(0);
  await expect(history.getByTestId("problem-revision-row")).toHaveCount(4);

  // The list, filtered by status.
  await main(page).getByTestId("problem-back").click();
  await expect(page).toHaveURL(new RegExp(`${problemsPath(project)}$`));
  await expect(main(page).getByTestId("problems-table").locator("tbody tr")).toHaveCount(2);
  await main(page).getByTestId("problem-statuses").locator('[data-facet-value="dropped"]').click();
  await expect(main(page).getByTestId("state-empty")).toContainText("No problem has this status");
  await expect(page).toHaveURL(/\?status=dropped$/);
});

test("a problem's page shows its metric, goals, claims by canvas, plans and Loop chains, and chips lead back to it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const seeded = await seedProblems(me, project);

  await open(page, problemsPath(project, "P-1"));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText(PROBLEM.statement);
  await expect(main(page).getByTestId("problem-workaround")).toHaveText(PROBLEM.workaround);
  const strip = main(page).getByTestId("problem-metric-strip");
  await expect(strip.getByTestId("summary-figure-value")).toHaveText("0.4567");
  await expect(strip.getByTestId("summary-latest-value")).toHaveText("0.5");
  await expect(strip.getByTestId("summary-signals-value")).toHaveText("3");
  await expect(strip.getByTestId("summary-figure-series").getByRole("img")).toHaveAttribute("aria-label", "3 values, from 0.42 to 0.5, lowest 0.42, highest 0.5");
  await expect(main(page).getByTestId("problem-goals").getByTestId("problem-goal")).toHaveAttribute("data-goal", "G-1");
  const canvas = main(page).getByTestId("problem-claims").getByTestId("problem-canvas");
  await expect(canvas).toHaveAttribute("data-canvas", "northwind");
  await expect(canvas.getByTestId("problem-claim")).toHaveAttribute("data-claim", "CL-1");
  await expect(canvas.getByTestId("claim-status")).toHaveText("Testing");
  const plan = main(page).getByTestId("problem-plans").getByTestId("problem-plan");
  await expect(plan).toHaveAttribute("data-plan-id", RELEASED_PLAN);
  await expect(plan.getByTestId("problem-plan-progress")).toHaveText("1/1 steps done");
  const loop = main(page).getByTestId("problem-loop");
  await expect(loop.getByTestId("chain-item")).toHaveCount(11);
  const discover = loop.locator(`[data-testid="chain-item"][data-item-id="${seeded.items.discover}"]`);
  await expect(discover.getByTestId("chain-item-link")).toHaveText("Discover for CL-1");
  await expect(discover.getByTestId("item-kind-icon")).toHaveAttribute("data-kind", "discover");
  await bothThemes(page, "a problem's page");

  // The chain of a work item starts from the problem.
  await open(page, itemPath(project, seeded.items.build));
  const stage = main(page).getByTestId("chain-rail").locator('[data-stage="problem"]');
  await expect(stage).toHaveAttribute("data-reached", "true");
  await expect(stage.getByTestId("chain-problem")).toHaveText("P-1");

  // The chips of the plans list, a plan, a goal and a claim each open the problem.
  await open(page, `/p/${project}/plans`);
  const row = main(page).getByTestId("plans-table-active").locator("tbody tr").filter({ hasText: RELEASED_PLAN });
  await expect(row.getByTestId("problem-chip")).toHaveAccessibleName(`Problem P-1: ${PROBLEM.statement}`);
  await row.getByTestId("problem-chip").click();
  await expect(page).toHaveURL(new RegExp(`${problemsPath(project, "P-1")}$`));

  await open(page, `/p/${project}/plans/${RELEASED_PLAN}`);
  await expect(main(page).getByTestId("plan-problems").getByTestId("problem-chip")).toHaveText("P-1");

  await open(page, goalsPath(project, "G-1"));
  await expect(main(page).getByTestId("goal-problems").getByTestId("problem-chip")).toHaveText("P-1");
  await expect(main(page).getByTestId("goal-problem")).toContainText(PROBLEM.statement);

  await open(page, claimPath(project, "CL-1"));
  await main(page).getByTestId("claim-problems").getByTestId("problem-chip").click();
  await expect(page).toHaveURL(new RegExp(`${problemsPath(project, "P-1")}$`));
});

test("a reader reads the problems and finds nothing that writes", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const owner = newAccount("owner");
  await new HubAdmin().grant(project, owner.login, "writer", "internal");
  await putProblemByApi(owner, project, "P-1", { ...PROBLEM, metric: null });

  await open(page, problemsPath(project));
  await expect(main(page).getByTestId("problems-table").locator("tbody tr")).toHaveCount(1);
  await expect(main(page).getByTestId("problems-new")).toHaveCount(0);
  await main(page).getByTestId("problem-link").click();
  await expect(main(page).getByTestId("problem-read-only")).toBeVisible();
  await expect(main(page).getByTestId("problem-edit")).toHaveCount(0);
  await expect(main(page).getByTestId("problem-close")).toHaveCount(0);
  await expect(main(page).getByTestId("problem-owner")).toHaveText(owner.login);
  await expect(main(page).getByTestId("problem-metric")).toContainText("This problem names no metric");
  await bothThemes(page, "a problem a reader reads");
});

test("the problems list and a problem's page on a phone", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedProblems(me, project);
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, problemsPath(project));
  await expect(main(page).getByTestId("problems-table")).toHaveAttribute("data-layout", "list");
  await expectNoSeriousViolations(page, "the problems list at 375 px");
  await open(page, problemsPath(project, "P-1"));
  await expect(main(page).getByTestId("problem-loop").getByTestId("chain-item").first()).toBeVisible();
  const sideways = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(sideways).toBeLessThanOrEqual(0);
  await expectNoSeriousViolations(page, "a problem's page at 375 px");
});
