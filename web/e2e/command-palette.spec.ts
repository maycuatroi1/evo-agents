import type { Locator, Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount } from "./support/hub";
import { open } from "./support/plans";
import { dispatch, RUN_PLAN, runPath, seedRunPlan, STEP_TITLES } from "./support/runs";

/**
 * The command palette against the real API: Cmd K (Ctrl K off Apple) and the top bar's field open it, Esc gives focus
 * back to what held it, and "/" still focuses the page's search. Dispatch a step and Run plan open the dialogs their
 * pages open; #N goes to a run through the runs list's q; Tab switches the project; a reader is offered no action.
 * Pages render in English.
 */
test.skip(isDeployed, "seeds plans and runs through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** The palette once the shell has hydrated (the trigger's key shows only then) and the shortcut has opened it. */
async function openWithKeyboard(page: Page): Promise<Locator> {
  await expect(page.getByTestId("palette-trigger-key")).toBeAttached();
  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByTestId("command-palette");
  await expect(palette).toBeVisible();
  await expect(palette.getByRole("combobox", { name: "Search or run a command" })).toBeFocused();
  return palette;
}

function option(palette: Locator, item: string): Locator {
  return palette.locator(`[data-item="${item}"]`);
}

test("opens with the keyboard or the top bar's field, and Esc gives focus back", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await dispatch(me, project, ["2"]);
  await open(page, `/p/${project}/runs`);
  await expect(main(page).getByTestId("runs-table")).toBeVisible();

  // "/" still focuses the list's search; Cmd K opens the palette over it and Esc returns there.
  await expect(page.getByTestId("palette-trigger-key")).toBeAttached();
  await page.keyboard.press("/");
  const search = main(page).getByTestId("runs-search");
  await expect(search).toBeFocused();
  const palette = await openWithKeyboard(page);
  await expect(page.getByTestId("palette-trigger")).toHaveAttribute("aria-expanded", "true");
  await expect(palette.getByRole("listbox")).toBeVisible();
  await expect(palette.getByTestId("palette-scope")).toHaveText(project);
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expect(search).toBeFocused();

  // "/" typed inside the palette is text, not the page's shortcut.
  await search.blur();
  await openWithKeyboard(page);
  await page.keyboard.type("/");
  await expect(palette.getByRole("combobox")).toHaveValue("/");
  // The shortcut closes it again.
  await page.keyboard.press("ControlOrMeta+k");
  await expect(palette).toBeHidden();

  // The top bar's field: "Search or jump to", and focus comes back to it.
  const trigger = page.getByTestId("palette-trigger");
  await expect(trigger).toHaveAccessibleName("Search or jump to");
  await trigger.click();
  await expect(palette).toBeVisible();
  await expect(palette.getByRole("combobox")).toBeFocused();
  await expect(palette.getByRole("combobox")).toHaveValue("");
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
  await expect(trigger).toBeFocused();
});

test("Dispatch a step and Run plan open the dialogs of the project", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await open(page, `/p/${project}`);

  // Actions come first; Dispatch a step opens the dispatch dialog with the plan's ready steps.
  let palette = await openWithKeyboard(page);
  const actions = palette.getByTestId("palette-group-actions");
  await expect(actions.getByRole("option")).toHaveText([/^Run plan Worker fleet rollout/, /^Dispatch a step/, /^New plan/, /^Register worker/]);
  await expect(option(palette, `action:plan-run:${project}:${RUN_PLAN}`)).toContainText("4 steps left");
  await page.keyboard.type("dispatch");
  await expect(actions.getByRole("option")).toHaveText([/^Dispatch a step/]);
  await expect(option(palette, `action:dispatch:${project}`)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(palette).toBeHidden();
  const dispatchDialog = page.getByTestId("dispatch-dialog");
  await expect(dispatchDialog).toBeVisible();
  await expect(dispatchDialog.getByTestId("dispatch-step-2")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dispatchDialog).toBeHidden();

  // Run plan, picked with the arrow keys and Enter, opens the plan's Run plan dialog.
  palette = await openWithKeyboard(page);
  await page.keyboard.type("run plan");
  await expect(option(palette, `action:plan-run:${project}:${RUN_PLAN}`)).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("plan-run-dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("plan-run-dialog")).toBeHidden();

  // The top bar's field opens Register worker the same way, and focus comes back to the field.
  await page.getByTestId("palette-trigger").click();
  await expect(palette.getByRole("combobox")).toBeFocused();
  await page.keyboard.type("register");
  await expect(option(palette, "action:register")).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("register-dialog")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("register-dialog")).toBeHidden();
  await expect(page.getByTestId("palette-trigger")).toBeFocused();
});

test("goes to a run by #id, and Tab looks in every project", async ({ page, member }) => {
  const me = await member([
    { role: "writer", maxLevel: "internal" },
    { role: "writer", maxLevel: "internal" },
  ]);
  const [project, other] = me.projects;
  await seedRunPlan(me, project);
  const [first, second] = await dispatch(me, project, ["2", "4"]);
  await open(page, `/p/${project}/plans`);

  // The hub finds the run by its number in the project's runs (q=#N), and Enter opens its page.
  let palette = await openWithKeyboard(page);
  await page.keyboard.type(`#${second.id}`);
  const runs = palette.getByTestId("palette-group-runs");
  await expect(runs.getByRole("option")).toHaveCount(1);
  const found = option(palette, `run:${project}:${second.id}`);
  await expect(found).toContainText(`#${second.id}`);
  await expect(found).toContainText(STEP_TITLES["4"]);
  await expect(found).toContainText("Queued");
  await expect(found).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`${runPath(project, second.id)}$`));
  await expect(main(page).getByRole("heading", { level: 1 })).toBeVisible();

  // Tab moves to the next project, then to every project, where the overview's runs are found with their project.
  palette = await openWithKeyboard(page);
  const scope = palette.getByTestId("palette-scope");
  const cycle = [...[project, other].sort(), ""];
  await expect(scope).toHaveText(project);
  await page.keyboard.type(`#${first.id}`);
  await expect(option(palette, `run:${project}:${first.id}`)).toBeVisible();
  for (const next of cycle.slice(cycle.indexOf(project) + 1)) {
    await page.keyboard.press("Tab");
    await expect(scope).toHaveAttribute("data-scope", next);
  }
  await expect(scope).toHaveText("All projects");
  await expect(palette.getByRole("combobox")).toBeFocused();
  await expect(option(palette, `run:${project}:${first.id}`)).toContainText(`${project}, Queued`);
  await page.keyboard.press("Shift+Tab");
  await expect(scope).toHaveText(cycle[1]);
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();
});

test("a reader finds the project's runs and plans but is offered no action", async ({ page, member, admin }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const writer = newAccount("writer");
  await admin.grant(project, writer.login, "writer", "internal");
  await seedRunPlan(writer, project);
  const [run] = await dispatch(writer, project, ["2"]);
  await open(page, `/p/${project}`);

  const palette = await openWithKeyboard(page);
  await expect(palette.getByTestId("palette-group-plans").getByRole("option")).toHaveText([/^Worker fleet rollout/]);
  await expect(palette.getByTestId("palette-group-goto").getByRole("option").first()).toHaveText(new RegExp(`^Overview of ${project}`));
  await expect(palette.getByTestId("palette-group-actions")).toHaveCount(0);
  await page.keyboard.type(`#${run.id}`);
  await expect(option(palette, `run:${project}:${run.id}`)).toBeVisible();
  for (const word of ["dispatch", "run plan", "rerun", "register"]) {
    await palette.getByRole("combobox").fill(word);
    await expect(palette.getByTestId("palette-count")).not.toHaveText("Searching runs");
    await expect(palette.getByTestId("palette-group-actions")).toHaveCount(0);
  }
  await expect(palette.getByRole("option", { name: /Dispatch a step|Run plan|Rerun|Register worker/ })).toHaveCount(0);
});

test("goes to the Monitor, in every project's Go to", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  await open(page, `/p/${me.projects[0]}`);
  const palette = await openWithKeyboard(page);
  // Among the hub's pages while nothing is typed, after Home and Inbox.
  const hub = palette.getByTestId("palette-group-goto").locator('[data-item^="goto:hub:"]');
  await expect(hub.first()).toHaveAttribute("data-item", "goto:hub:home");
  await expect(hub.nth(1)).toHaveAttribute("data-item", "goto:hub:inbox");
  await expect(hub.nth(2)).toHaveAttribute("data-item", "goto:hub:monitor");
  await page.keyboard.type("monitor");
  await expect(option(palette, "goto:hub:monitor")).toHaveAttribute("aria-selected", "true");
  await expect(option(palette, "goto:hub:monitor")).toContainText("Monitor");
  await page.keyboard.press("Enter");
  await expect(palette).toBeHidden();
  await page.waitForURL(/\/monitor$/);
  await expect(main(page).getByRole("heading", { level: 1 })).toHaveText("Monitor");
});
