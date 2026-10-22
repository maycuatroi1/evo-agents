import type { Page } from "@playwright/test";

import { ceilingOver, prefByApi, stopOwnByApi } from "./support/autonomy";
import { expect, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { runningRuns, seedMonitorPlan } from "./support/monitor";
import { seatByApi, seatOf, unitByApi } from "./support/org";
import { dispatch, openRunDetails, runPath, seedRunPlan } from "./support/runs";

/**
 * Autonomy in the web (plan autonomy-levels, step 7): a person's own settings at /settings/autonomy, the autonomy a run
 * acts under on its page, and a stop with its lift on the Org page. Each test makes its own people and units, as the
 * chart is shared by every spec; none stops the whole hub, which would stop the runs of the specs beside it.
 */

test.describe.configure({ mode: "parallel" });

const main = (page: Page) => page.locator("#main");

test("a member picks a level under the ceiling by keyboard, sees the levels above it dimmed with their reason, and saves", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member();
  const slug = await ceilingOver(me.login, "A2");
  await page.goto("/settings/autonomy");

  await expect(page.getByRole("heading", { level: 1, name: "Autonomy" })).toBeVisible();
  // Nothing unusual yet: no preference is the one note, and no stop or lowered class is flagged.
  await expect(main(page).getByTestId("autonomy-no-pref")).toBeVisible();
  await expect(main(page).getByTestId("autonomy-stop")).toHaveCount(0);
  await expect(main(page).getByTestId("autonomy-lowered")).toHaveCount(0);
  await expect(main(page).getByTestId("autonomy-level")).toHaveText("Level A2");

  // The levels above the ceiling stay in the list, out of reach, with the ceiling and the unit that sets it.
  const options = main(page).getByTestId("autonomy-level-option");
  await expect(options).toHaveCount(6);
  for (const level of ["A3", "A4", "A5"]) {
    const option = options.and(page.locator(`[data-level="${level}"]`));
    await expect(option).toHaveAttribute("data-above", "true");
    await expect(option.getByTestId("autonomy-level-why")).toHaveText(`Above your ceiling A2, set by unit ${slug} (team Unit ${slug})`);
    await expect(option.getByRole("radio")).toBeDisabled();
  }
  for (const level of ["A0", "A1", "A2"]) await expect(options.and(page.locator(`[data-level="${level}"]`))).not.toHaveAttribute("data-above", /.+/);

  // A class's modes above its ceiling are dimmed too: K6 stops at Ask under A2.
  const k6 = main(page).locator('[data-testid="autonomy-class"][data-class="K6"]');
  await expect(k6.getByTestId("autonomy-class-ceiling")).toHaveText(`Ceiling Ask, set by unit ${slug}`);
  await expect(k6.getByTestId("autonomy-class-select").locator('option[value="auto"]')).toBeDisabled();
  await expect(k6.getByTestId("autonomy-class-select").locator('option[value="ask"]')).toBeEnabled();

  // By keyboard: from A1 the arrow moves to A2 and no further, past the levels out of reach.
  const save = main(page).getByTestId("autonomy-save");
  await expect(save).toBeDisabled();
  await page.getByRole("radio", { name: "A1: Branch work" }).focus();
  await page.keyboard.press("Space");
  await expect(page.getByRole("radio", { name: "A1: Branch work" })).toBeChecked();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("radio", { name: "A2: Supervised" })).toBeChecked();
  await expect(page.getByRole("radio", { name: "A2: Supervised" })).toBeFocused();

  const k10 = main(page).locator('[data-testid="autonomy-class"][data-class="K10"]');
  await k10.getByTestId("autonomy-class-select").selectOption("deny");
  await expect(save).toBeEnabled();
  await save.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("toast").filter({ hasText: "Preference saved: A2, revision 1" })).toBeVisible();

  await page.reload();
  await expect(main(page).getByTestId("autonomy-no-pref")).toHaveCount(0);
  await expect(page.getByRole("radio", { name: "A2: Supervised" })).toBeChecked();
  await expect(k10.getByTestId("autonomy-class-mode")).toHaveAttribute("data-mode", "deny");
  await expect(k10.getByTestId("autonomy-class-source")).toHaveText("set by your preference");
  await expect(k10.getByTestId("autonomy-class-select")).toHaveValue("deny");
  await expect(save).toBeDisabled();
});

test("a ceiling below the preference flags the classes it lowers, and a stop that holds shows", async ({ page, member }) => {
  const me = await member();
  await ceilingOver(me.login, "A2");
  await prefByApi(me, { level: "A4" });
  await page.goto("/settings/autonomy");

  await expect(main(page).getByTestId("autonomy-lowered")).toBeVisible();
  const k6 = main(page).locator('[data-testid="autonomy-class"][data-class="K6"]');
  await expect(k6).toHaveAttribute("data-lowered", "true");
  await expect(k6.getByTestId("autonomy-class-lowered")).toHaveText("Lowered from Auto");
  // A class the ceiling leaves as the preference set it carries no flag.
  const k1 = main(page).locator('[data-testid="autonomy-class"][data-class="K1"]');
  await expect(k1).not.toHaveAttribute("data-lowered", /.+/);

  await stopOwnByApi(me, { reason: "checking a leak" });
  await page.reload();
  const stop = main(page).getByTestId("autonomy-stop");
  await expect(stop).toContainText("Your runs are stopped");
  await expect(stop).toContainText("checking a leak");
  await stopOwnByApi(me, { lift: true });
});

test("a run's page shows whom it acts for and the autonomy the hub took for it", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const [run] = await dispatch(me, project, ["2"]);
  await page.goto(runPath(project, run.id));
  await openRunDetails(page);

  const card = main(page).getByTestId("run-autonomy");
  await expect(card).toBeVisible();
  await expect(card.getByTestId("run-principal")).toHaveText(me.login);
  await expect(card.getByTestId("run-autonomy-level")).toHaveText(/^A5, set by /);
  // At A5 the preference leaves every class auto; the policy of the project's repo api asks before K5, so that class
  // alone is listed, with the policy as its source. Nothing tightened the run.
  const bounded = card.getByTestId("run-autonomy-bounded").getByTestId("run-autonomy-class");
  await expect(bounded).toHaveCount(1);
  await expect(bounded).toHaveAttribute("data-class", "K5");
  await expect(bounded.locator("[data-mode]")).toHaveAttribute("data-mode", "ask");
  await expect(bounded).toContainText(`the policy of ${project}/api`);
  await expect(card.getByTestId("run-autonomy-tightened")).toHaveCount(0);
  await card.getByText("Show all 12 classes").click();
  await expect(card.getByTestId("run-autonomy-all").getByTestId("run-autonomy-class")).toHaveCount(12);
});

test("a hub admin stops a person's runs from the Org page, told how many, and lifts the stop", async ({ page, admin, signInAs }) => {
  test.setTimeout(60_000);
  const project = uniqueName("auto-proj");
  const person = newAccount("auto-stop");
  await admin.registerProject(project);
  await admin.grant(project, person.login, "writer", "internal");
  const slug = await unitByApi("auto-stop");
  await seatByApi({ unit: slug, role: "member", login: person.login, primary: true });
  await seedMonitorPlan(person, project);
  await runningRuns(person, project, ["1"]);
  await signInAs(ADMIN_ACCOUNT);
  await page.goto("/org");

  const seat = seatOf(page, person.login, slug);
  // The seat's mode of each class, behind its disclosure.
  const classes = seat.getByTestId("org-seat-autonomy");
  await expect(classes).toHaveAttribute("aria-expanded", "false");
  await classes.click();
  await expect(seat.getByTestId("org-seat-class")).toHaveCount(12);
  await expect(seat.getByTestId("org-seat-stopped")).toHaveCount(0);

  await seat.getByTestId("org-seat-menu").click();
  await page.getByTestId("org-action-stop-runs").click();
  const dialog = page.getByTestId("org-stop");
  await expect(dialog).toContainText(`Stop the runs of ${person.login}?`);
  await expect(dialog.getByTestId("org-stop-runs")).toHaveText("1 run at work will be cancelled or asked to stop, 1 person covered.");
  await dialog.getByTestId("org-stop-confirm").click();
  await expect(dialog).toBeHidden();

  const stopped = seat.getByTestId("org-seat-stopped");
  await expect(stopped).toContainText("Stopped");
  await stopped.getByTestId("org-seat-stopped-lift").click();
  await expect(seat.getByTestId("org-seat-stopped")).toHaveCount(0);
  await expect(page.getByTestId("toast").filter({ hasText: `Stop of ${person.login} lifted` })).toBeVisible();
});
