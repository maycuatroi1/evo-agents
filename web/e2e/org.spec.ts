import { expect, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, newAccount, uniqueName } from "./support/hub";
import { runningRuns, seedMonitorPlan } from "./support/monitor";
import { dayFromNow, endSeatByApi, menuActions, seatByApi, seatOf, unitByApi, unitHead, unitOf, unseatByApi } from "./support/org";

/**
 * The Org page (plan org-chart, criterion a6): a hub admin builds the chart from the page, a unit's lead manages what
 * lies below their unit and nothing above it, and a member reads it without a single write button. Each test makes
 * units of its own with unique slugs, as the chart is shared by every spec, and its people are made-up accounts.
 */

test.describe.configure({ mode: "parallel" });

test("a hub admin creates a unit, places an unplaced person, sets a ceiling, meets the hub's 409 and 422 and ends the seat", async ({ page, admin, signInAs }) => {
  test.setTimeout(60_000);
  const project = uniqueName("org-proj");
  const [pat, quinn, nobody] = [newAccount("org-pat"), newAccount("org-quinn"), newAccount("org-nobody")];
  await admin.registerProject(project);
  for (const person of [pat, quinn, nobody]) await admin.grant(project, person.login, "reader", "internal");
  // Every account holds a seat at the root from its grant on; Nobody's ends, so they hold none to report to.
  await unseatByApi(nobody.login);
  await signInAs(ADMIN_ACCOUNT);
  await page.goto("/org");
  await expect(page.getByRole("heading", { level: 1, name: "Org chart" })).toBeVisible();

  const unplaced = page.locator("#main").getByTestId("org-unplaced");
  await expect(unplaced.locator(`[data-login="${pat.login}"]`)).toBeVisible();

  // A new unit below the root, from the header's button.
  const slug = uniqueName("org-team");
  await page.locator("#main").getByTestId("org-new-unit").click();
  const unitDialog = page.getByTestId("org-unit-dialog");
  await unitDialog.getByTestId("org-unit-slug").fill("Not a slug");
  await unitDialog.getByTestId("org-unit-dialog-save").click();
  await expect(unitDialog.getByTestId("org-unit-slug")).toHaveAttribute("aria-invalid", "true");
  await unitDialog.getByTestId("org-unit-slug").fill(slug);
  await unitDialog.getByTestId("org-unit-name").fill("Platform team");
  await unitDialog.getByTestId("org-unit-dialog-save").click();
  await expect(unitDialog).toBeHidden();
  await expect(unitOf(page, slug).getByTestId("org-unit-name").first()).toHaveText("Platform team");

  // Place Pat from the Unplaced list, in the new unit.
  await unplaced.locator(`[data-login="${pat.login}"]`).getByTestId("org-unplaced-place").click();
  const seatDialog = page.getByTestId("org-seat-dialog");
  await expect(seatDialog.getByTestId("org-seat-login")).toHaveValue(pat.login);
  await seatDialog.getByTestId("org-seat-unit").selectOption(slug);
  await seatDialog.getByTestId("org-seat-dialog-save").click();
  await expect(seatDialog).toBeHidden();
  const seat = seatOf(page, pat.login, slug);
  await expect(seat.getByTestId("org-seat-role")).toHaveText("Member");
  await expect(unplaced.locator(`[data-login="${pat.login}"]`)).toHaveCount(0);
  // An ordinary seat carries no status indicator.
  await expect(seat).not.toHaveAttribute("data-note", /.+/);

  // The seat's ceiling, with its level and where it comes from.
  await seat.getByTestId("org-seat-menu").click();
  await page.getByTestId("org-action-seat-ceiling").click();
  const ceilingDialog = page.getByTestId("org-ceiling-dialog");
  await ceilingDialog.getByTestId("org-level").selectOption("A2");
  await ceilingDialog.getByTestId("org-ceiling-note").fill("Trial period");
  await ceilingDialog.getByTestId("org-ceiling-dialog-save").click();
  await expect(ceilingDialog).toBeHidden();
  await expect(seat.getByTestId("org-ceiling")).toHaveText("ceiling A2");
  await expect(seat.getByTestId("org-effective")).toHaveText("Level A2");
  await expect(seat.getByTestId("org-level-line")).toContainText("from this seat");

  // A second seat for Pat in the same unit: the hub refuses with 409, and its reason shows in the dialog.
  await unitHead(page, slug).getByTestId("org-unit-menu").click();
  await page.getByTestId("org-action-add-seat").click();
  await seatDialog.getByTestId("org-seat-login").fill(pat.login);
  await seatDialog.getByTestId("org-seat-dialog-save").click();
  const refusal = seatDialog.getByRole("alert");
  await expect(refusal).toContainText("The chart changed or this already exists.");
  await expect(refusal).toContainText(`Hub: login=${pat.login} holds a live seat in ${slug} already`);
  await page.keyboard.press("Escape");
  await expect(seatDialog).toBeHidden();

  // Quinn reporting to someone without a seat: the hub refuses with 422, and says who.
  await unitHead(page, slug).getByTestId("org-unit-menu").click();
  await page.getByTestId("org-action-add-seat").click();
  await seatDialog.getByTestId("org-seat-login").fill(quinn.login);
  await seatDialog.getByTestId("org-seat-reports-to").fill(nobody.login);
  await seatDialog.getByTestId("org-seat-dialog-save").click();
  await expect(seatDialog.getByRole("alert")).toContainText(`Hub: ${nobody.login} holds no live seat to report to`);
  await page.keyboard.press("Escape");
  await expect(seatDialog).toBeHidden();

  // End the seat: it leaves the tree, and comes back marked Ended with the archived and ended entries shown.
  await seat.getByTestId("org-seat-menu").click();
  await page.getByTestId("org-action-end-seat").click();
  await page.getByTestId("org-confirm-confirm").click();
  await expect(page.getByTestId("org-confirm")).toBeHidden();
  await expect(seat).toHaveCount(0);
  await page.locator("#main").getByTestId("org-show-all").click();
  await expect(seat.getByTestId("org-seat-ended")).toBeVisible();
  await expect(seat.getByTestId("org-seat-menu")).toHaveCount(0);
});

test("a seat that ends within 7 days says so, and one that ends later says nothing", async ({ page, admin, signInAs }) => {
  const project = uniqueName("org-proj");
  const [soon, later] = [newAccount("org-soon"), newAccount("org-later")];
  await admin.registerProject(project);
  for (const person of [soon, later]) await admin.grant(project, person.login, "reader", "internal");
  const slug = await unitByApi("org-cohort");
  await seatByApi({ unit: slug, role: "member", login: soon.login, ends_on: dayFromNow(3) });
  await seatByApi({ unit: slug, role: "member", login: later.login, ends_on: dayFromNow(30) });
  await signInAs(ADMIN_ACCOUNT);
  await page.goto("/org");
  await expect(seatOf(page, soon.login, slug).getByTestId("org-seat-ends-soon")).toHaveText("Ends in 3 days");
  await expect(seatOf(page, later.login, slug)).toBeVisible();
  await expect(seatOf(page, later.login, slug).getByTestId("org-seat-ends-soon")).toHaveCount(0);
});

test("a lead manages the units below theirs and the member seats of their own, never above it, and sees the hub's 403", async ({ page, admin, signInAs }) => {
  test.setTimeout(60_000);
  const project = uniqueName("org-proj");
  const [lead, newcomer, peer] = [newAccount("org-lead"), newAccount("org-new"), newAccount("org-peer")];
  await admin.registerProject(project);
  for (const person of [lead, newcomer, peer]) await admin.grant(project, person.login, "reader", "internal");
  const team = await unitByApi("org-led");
  const below = await unitByApi("org-below", team);
  const leadSeat = await seatByApi({ unit: team, role: "lead", login: lead.login });
  await signInAs(lead);
  await page.goto("/org");

  await expect(unitOf(page, team)).toBeVisible();
  await expect(page.locator("#main").getByTestId("org-unplaced")).toHaveCount(0);
  await expect(page.locator("#main").getByTestId("org-new-unit")).toHaveCount(0);
  await expect(unitHead(page, "root").getByTestId("org-unit-menu")).toHaveCount(0);
  expect(await menuActions(page, unitHead(page, team).getByTestId("org-unit-menu"))).toEqual(["create-unit", "add-seat"]);
  // A unit closes and opens again from its row, by keyboard.
  const toggle = unitHead(page, below).getByTestId("org-unit-toggle");
  await toggle.focus();
  await expect(toggle).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Space");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  expect(await menuActions(page, unitHead(page, below).getByTestId("org-unit-menu"))).toEqual(["create-unit", "add-seat", "edit-unit", "unit-ceiling", "archive-unit"]);
  // Their own seat, a lead's in the unit they lead: neither theirs to change nor its ceiling theirs to set.
  await expect(seatOf(page, lead.login, team)).toBeVisible();
  await expect(seatOf(page, lead.login, team).getByTestId("org-seat-menu")).toHaveCount(0);

  // A member seat in the unit below, from its menu; the role offers every role there.
  await unitHead(page, below).getByTestId("org-unit-menu").click();
  await page.getByTestId("org-action-add-seat").click();
  const seatDialog = page.getByTestId("org-seat-dialog");
  await seatDialog.getByTestId("org-seat-login").fill(newcomer.login);
  await seatDialog.getByTestId("org-seat-reports-to").fill(lead.login);
  await seatDialog.getByTestId("org-seat-dialog-save").click();
  await expect(seatDialog).toBeHidden();
  await expect(seatOf(page, newcomer.login, below).getByTestId("org-seat-who")).toHaveText(newcomer.login);
  await expect(seatOf(page, newcomer.login, below)).toContainText(`Reports to ${lead.login}`);

  // The hub admin ends the lead's seat meanwhile: the page still offers the write, and the hub's 403 says why not.
  await endSeatByApi(leadSeat.id);
  await unitHead(page, team).getByTestId("org-unit-menu").click();
  await page.getByTestId("org-action-add-seat").click();
  await seatDialog.getByTestId("org-seat-login").fill(peer.login);
  await seatDialog.getByTestId("org-seat-dialog-save").click();
  const refusal = seatDialog.getByRole("alert");
  await expect(refusal).toContainText("The hub refused this change.");
  await expect(refusal).toContainText("Hub: ");
});

test("a member reads the chart, the runs at work of the people in it, and finds no write button", async ({ page, member }) => {
  test.setTimeout(60_000);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const slug = await unitByApi("org-read");
  await seatByApi({ unit: slug, role: "member", login: me.login });
  await seedMonitorPlan(me, project);
  await runningRuns(me, project, ["1"]);
  await page.goto("/org");

  const seat = seatOf(page, me.login, slug);
  await expect(seat.getByTestId("org-seat-role")).toHaveText("Member");
  await expect(seat.getByTestId("org-effective")).toHaveText(/^Level A\d$/);
  const runs = seat.getByTestId("org-seat-runs");
  await expect(runs).toHaveText(`1 run at work in ${project}`);
  await expect(runs).toHaveAttribute("href", `/p/${project}/runs?state=active&q=${me.login}`);

  const main = page.locator("#main");
  await expect(main.getByTestId("org-new-unit")).toHaveCount(0);
  await expect(main.getByTestId("org-unit-menu")).toHaveCount(0);
  await expect(main.getByTestId("org-seat-menu")).toHaveCount(0);
  await expect(main.getByTestId("org-unplaced")).toHaveCount(0);

  await runs.click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/runs\\?state=active&q=${me.login}$`));
});
