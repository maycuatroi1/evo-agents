import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, newAccount } from "./support/hub";
import { goalsPath } from "./support/loop";
import { dayFromToday, OPPORTUNITY, opportunitiesPath, putOpportunityByApi, seedOpportunities } from "./support/opportunities";
import { open } from "./support/plans";
import { toast } from "./support/toast";

/**
 * The Opportunities pages of a project (plan opportunity-intake, criterion a6), against the real API: the list by the
 * nearest closing window with the days left and the problems each answers; an opportunity's page with its fields, its
 * evidence by signal, the problems and goals that answer it and its history; a writer adds, edits, closes and reopens
 * one; a reader reads and writes nothing. axe checks the pages in light and dark.
 */
test.skip(isDeployed, "seeds the opportunities through the local stack");

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

test("the list puts the nearest window first and the opportunity with no date last", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedOpportunities(me, project);

  await open(page, opportunitiesPath(project));
  await expect(main(page).getByRole("heading", { level: 1, name: "Opportunities" })).toBeVisible();
  await expect(page.getByTestId("nav-opportunities")).toHaveAttribute("aria-current", "page");
  const table = main(page).getByTestId("opportunities-table");
  await expect(table.getByTestId("opportunity-link")).toHaveCount(3);
  await expect(table.getByTestId("opportunity-link")).toHaveText([
    "Join the city's market-day programme",
    OPPORTUNITY.statement,
    "Resell the checkout widget to the shop-fitting suppliers",
  ]);
  await expect(table.getByTestId("opportunity-status").first()).toHaveText("Open");
  await expect(table.getByTestId("opportunity-closes").first()).toContainText("5 days left");
  await expect(table.getByTestId("opportunity-closes").nth(1)).toContainText("20 days left");
  await expect(table.locator("tbody tr").nth(1).getByTestId("opportunity-answers")).toContainText("P-1");
  await bothThemes(page, "the opportunities list");

  await main(page).getByTestId("opportunity-statuses").locator('[data-facet-value="missed"]').click();
  await expect(main(page).getByTestId("state-empty")).toContainText("No opportunity has this status");
  await expect(page).toHaveURL(/\?status=missed$/);
});

test("an opportunity's page shows its fields, evidence by signal, answers and goals", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedOpportunities(me, project);

  await open(page, opportunitiesPath(project, "O-1"));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText(OPPORTUNITY.statement);
  await expect(main(page).getByTestId("opportunity-head").getByTestId("opportunity-status")).toHaveText("Open");
  await expect(main(page).getByTestId("opportunity-beneficiary")).toHaveText(OPPORTUNITY.beneficiary);
  await expect(main(page).getByTestId("opportunity-leverage")).toHaveText(OPPORTUNITY.leverage);
  await expect(main(page).getByTestId("opportunity-window")).toContainText(OPPORTUNITY.window_note);
  await expect(main(page).getByTestId("opportunity-days-left")).toContainText("20 days left");
  await expect(main(page).getByTestId("opportunity-signal")).toHaveCount(2);
  await expect(main(page).getByTestId("opportunity-signal").first()).toHaveAttribute("data-signal", "wish");
  await expect(main(page).getByTestId("opportunity-signal").nth(1)).toHaveAttribute("data-signal", "external_demand");
  await expect(main(page).getByTestId("opportunity-evidence-line")).toHaveCount(2);
  await expect(main(page).getByTestId("opportunity-answers-panel")).toContainText("P-1");
  await expect(main(page).getByTestId("opportunity-goals").getByTestId("opportunity-goal")).toHaveAttribute("data-goal", "G-1");
  await bothThemes(page, "an opportunity's page");

  // The goal names the opportunity it answers.
  await open(page, goalsPath(project, "G-1"));
  await expect(main(page).getByTestId("goal-opportunity")).toContainText("O-1");
});

test("a writer adds an opportunity, edits it, closes it as missed and reopens it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedOpportunities(me, project);

  await open(page, opportunitiesPath(project));
  await main(page).getByTestId("opportunities-new").click();
  const dialog = page.getByTestId("opportunity-new-dialog");
  await expect(dialog.getByRole("heading", { name: "New opportunity" })).toBeVisible();
  await bothThemes(page, "the new opportunity dialog");
  await dialog.getByTestId("opportunity-new-form-submit").click();
  await expect(dialog.getByTestId("opportunity-form-wrong")).toHaveText("3 fields need a change before you save.");
  await expect(dialog.getByTestId("opportunity-statement-input")).toHaveAttribute("aria-invalid", "true");
  await dialog.getByTestId("opportunity-statement-input").fill("A stall at the winter fair");
  await dialog.getByTestId("opportunity-beneficiary-input").fill("Shops that sell gifts");
  await dialog.getByTestId("opportunity-leverage-input").fill("The fair offers a free corner to partners");
  await dialog.getByTestId("opportunity-window-until-input").fill(dayFromToday(30));
  await dialog.getByTestId("opportunity-answers-input").fill("P-1");
  await dialog.getByTestId("opportunity-new-form-submit").click();
  await expect(toast(page, "O-4 added")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${opportunitiesPath(project, "O-4")}$`));
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("A stall at the winter fair");
  await expect(main(page).getByTestId("opportunity-days-left")).toContainText("30 days left");
  await expect(main(page).getByTestId("opportunity-answers-panel")).toContainText("P-1");

  // An edit is the next revision; the history keeps both.
  await main(page).getByTestId("opportunity-edit").click();
  const edit = page.getByTestId("opportunity-edit-dialog");
  await edit.getByTestId("opportunity-leverage-input").fill("The fair offers a free corner and a stand");
  await edit.getByTestId("opportunity-edit-form-submit").click();
  await expect(toast(page, "O-4 saved as revision 2")).toBeVisible();
  await expect(main(page).getByTestId("opportunity-leverage")).toHaveText("The fair offers a free corner and a stand");
  await expect(main(page).getByTestId("opportunity-page")).toHaveAttribute("data-revision", "2");
  await expect(main(page).getByTestId("opportunity-revision-row")).toHaveCount(2);

  // Closed as missed, with why; then reopened.
  await main(page).getByTestId("opportunity-close").click();
  const close = page.getByTestId("opportunity-close-dialog");
  await close.getByTestId("opportunity-close-missed").click();
  await close.getByTestId("opportunity-close-submit").click();
  await expect(close.getByTestId("opportunity-close-reason-input")).toHaveAttribute("aria-invalid", "true");
  await close.getByTestId("opportunity-close-reason-input").fill("The fair gave the corner to another partner");
  await expect(close.getByTestId("opportunity-close-submit")).toHaveText("Close as missed");
  await close.getByTestId("opportunity-close-submit").click();
  await expect(toast(page, "O-4 is missed")).toBeVisible();
  await expect(main(page).getByTestId("opportunity-head").getByTestId("opportunity-status")).toHaveText("Missed");
  await expect(main(page).getByTestId("opportunity-closed")).toContainText("The fair gave the corner to another partner");
  await bothThemes(page, "a missed opportunity");

  await main(page).getByTestId("opportunity-reopen").click();
  await page.getByTestId("opportunity-reopen-dialog").getByTestId("opportunity-reopen-submit").click();
  await expect(toast(page, "O-4 is open again")).toBeVisible();
  await expect(main(page).getByTestId("opportunity-head").getByTestId("opportunity-status")).toHaveText("Open");
  await expect(main(page).getByTestId("opportunity-closed")).toHaveCount(0);
  await expect(main(page).getByTestId("opportunity-revision-row")).toHaveCount(4);
});

test("a reader reads the opportunities, a hidden answer is a count, and nothing writes", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const owner = newAccount("owner");
  await new HubAdmin().grant(project, owner.login, "writer", "internal");
  await putOpportunityByApi(owner, project, "O-1", { ...OPPORTUNITY, answers: [], window_until: dayFromToday(10) });

  await open(page, opportunitiesPath(project));
  await expect(main(page).getByTestId("opportunities-table").locator("tbody tr")).toHaveCount(1);
  await expect(main(page).getByTestId("opportunities-new")).toHaveCount(0);
  await main(page).getByTestId("opportunity-link").click();
  await expect(main(page).getByTestId("opportunity-read-only")).toBeVisible();
  await expect(main(page).getByTestId("opportunity-edit")).toHaveCount(0);
  await expect(main(page).getByTestId("opportunity-close")).toHaveCount(0);
  await bothThemes(page, "an opportunity a reader reads");
});

test("the opportunities list and an opportunity's page on a phone", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedOpportunities(me, project);
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, opportunitiesPath(project));
  await expect(main(page).getByTestId("opportunities-table")).toHaveAttribute("data-layout", "list");
  await expectNoSeriousViolations(page, "the opportunities list at 375 px");
  await open(page, opportunitiesPath(project, "O-1"));
  await expect(main(page).getByTestId("opportunity-signal").first()).toBeVisible();
  const sideways = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(sideways).toBeLessThanOrEqual(0);
  await expectNoSeriousViolations(page, "an opportunity's page at 375 px");
});
