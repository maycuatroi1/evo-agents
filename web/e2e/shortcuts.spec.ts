import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, test } from "./support/fixtures";
import { newAccount } from "./support/hub";
import { open } from "./support/plans";
import { dispatch, RUN_PLAN, seedRunPlan } from "./support/runs";

/**
 * The shell's keyboard shortcuts against the real API (components/shell/shortcuts.tsx): G then H, I, P, R or W goes
 * to Home, Inbox, the project's Plans and Runs, and Workers within a second; D opens Dispatch on a writer's project
 * pages (the page's own where it has one); ? lists every key, and turns single keys off and on. Keys typed in a text
 * field are text. The web terminal's own check is in terminal.spec.ts. Pages render in English.
 */
test.skip(isDeployed, "seeds projects and runs through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** Keys work once the shell has hydrated; the top bar's key shows only then. */
async function hydrated(page: Page) {
  await expect(page.getByTestId("palette-trigger-key")).toBeAttached();
}

/** G, then `key`: a person's pace, well within the second. */
async function goTo(page: Page, key: string) {
  await page.keyboard.press("g");
  await page.keyboard.press(key);
}

async function heading(page: Page) {
  await expect(main(page).getByRole("heading", { level: 1 })).toBeVisible();
}

test("G then a letter goes to Home, Inbox, Plans, Runs and Workers, and the sidebar shows the keys", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await open(page, `/p/${project}`);
  await hydrated(page);

  await goTo(page, "p");
  await expect(page).toHaveURL(new RegExp(`/p/${project}/plans$`));
  await heading(page);
  await goTo(page, "r");
  await expect(page).toHaveURL(new RegExp(`/p/${project}/runs$`));
  await heading(page);
  await goTo(page, "i");
  await expect(page).toHaveURL(/\/inbox$/);
  await heading(page);
  await goTo(page, "w");
  await expect(page).toHaveURL(/\/workers$/);
  await heading(page);
  await goTo(page, "h");
  await expect(page).toHaveURL(/\/$/);
  await heading(page);

  // On Home the member's only project is the one G P opens.
  await goTo(page, "p");
  await expect(page).toHaveURL(new RegExp(`/p/${project}/plans$`));
  await heading(page);

  // Each item G opens shows its keys while the pointer is over it.
  const inbox = page.getByTestId("nav-inbox");
  const keys = inbox.locator('[data-shortcut="goInbox"]');
  await expect(keys).toBeHidden();
  await inbox.hover();
  await expect(keys).toBeVisible();
  await expect(keys).toHaveText("GI");
  await expect(inbox).toHaveAccessibleName("Inbox");
});

test("the second key must come within a second, and a hint shows while G waits", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  await open(page, `/p/${me.projects[0]}`);
  await hydrated(page);
  const url = page.url();

  await page.keyboard.press("g");
  const hint = page.getByTestId("shortcuts-lead");
  await expect(hint).toBeVisible();
  await expect(hint).toContainText("Inbox");
  await expect(hint).toBeHidden({ timeout: 2_000 }); // the second passed
  await page.keyboard.press("i");
  await page.waitForTimeout(300); // a navigation would have started by now
  expect(page.url()).toBe(url);

  // Another key ends the sequence too.
  await page.keyboard.press("g");
  await page.keyboard.press("x");
  await page.keyboard.press("i");
  await page.waitForTimeout(300);
  expect(page.url()).toBe(url);
});

test("keys typed in a search field are text, and / still focuses it", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await dispatch(me, project, ["2"]);
  await open(page, `/p/${project}/runs`);
  await expect(main(page).getByTestId("runs-table")).toBeVisible();
  await hydrated(page);
  const url = page.url();

  await page.keyboard.press("/");
  const search = main(page).getByTestId("runs-search");
  await expect(search).toBeFocused();
  await page.keyboard.type("gi?dgw");
  await expect(search).toHaveValue("gi?dgw");
  expect(page.url().split("?")[0]).toBe(url.split("?")[0]);
  await expect(page.getByTestId("shortcuts-dialog")).toHaveCount(0);
  await expect(page.getByTestId("dispatch-dialog")).toHaveCount(0);

  // Keys with Ctrl or Cmd are the browser's: Ctrl G or Cmd G is its Find next, not G.
  await search.fill("");
  await search.blur();
  await page.keyboard.press("ControlOrMeta+g");
  await page.keyboard.press("i");
  await page.waitForTimeout(300);
  expect(page.url().split("?")[0]).toBe(url.split("?")[0]);
});

test("D opens Dispatch on a writer's project pages, the page's own where it has one", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await dispatch(me, project, ["2"]);

  // The project's overview has no Dispatch of its own: D opens the shell's, on the project's ready steps.
  await open(page, `/p/${project}`);
  await heading(page);
  await hydrated(page);
  await page.keyboard.press("d");
  const dialog = page.getByTestId("dispatch-dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.getByTestId("dispatch-step-4")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // The runs page shows the key on its Dispatch button, and D opens that button's dialog.
  await open(page, `/p/${project}/runs`);
  await hydrated(page);
  const button = main(page).getByTestId("runs-dispatch");
  await expect(button.locator('[data-shortcut="dispatch"]')).toHaveText("D");
  await expect(button).toHaveAttribute("aria-keyshortcuts", "D");
  await expect(button).toHaveAccessibleName("Dispatch");
  await button.focus();
  await page.keyboard.press("d");
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // A ready step's page: D runs that step, as its button does.
  await open(page, `/p/${project}/plans/${RUN_PLAN}/steps/4`);
  const section = main(page).getByTestId("step-runs");
  await expect(section.getByTestId("step-readiness")).toHaveText("Ready to run.");
  await expect(section.getByTestId("step-run").locator('[data-shortcut="dispatch"]')).toHaveText("D");
  await hydrated(page);
  await page.keyboard.press("d");
  await expect(dialog.getByTestId("dispatch-step-4").getByRole("checkbox")).toBeChecked();
  await expect(dialog.getByTestId("dispatch-step-2").getByRole("checkbox")).not.toBeChecked();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
});

test("D does nothing for a reader", async ({ page, member, admin }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const project = me.projects[0];
  const writer = newAccount("writer");
  await admin.grant(project, writer.login, "writer", "internal");
  await seedRunPlan(writer, project);
  await open(page, `/p/${project}/runs`);
  await heading(page);
  await hydrated(page);
  await page.keyboard.press("d");
  await page.waitForTimeout(300);
  await expect(page.getByTestId("dispatch-dialog")).toHaveCount(0);
  await expect(main(page).getByTestId("runs-dispatch")).toHaveCount(0);
});

test("? lists every key; single keys turn off and on, and stay so after a reload", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await dispatch(me, project, ["2"]);
  await open(page, `/p/${project}/runs`);
  await expect(main(page).getByTestId("runs-table")).toBeVisible();
  await hydrated(page);
  const url = page.url();

  await page.keyboard.press("Shift+Slash");
  const dialog = page.getByRole("dialog", { name: "Keyboard shortcuts" });
  await expect(dialog).toBeVisible();
  const rows = dialog.getByTestId("shortcut-row");
  await expect(rows).toHaveCount(13);
  await expect(dialog.locator('[data-shortcut="goRuns"]')).toContainText("G then R");
  await expect(dialog.locator('[data-shortcut="goCurator"]')).toContainText("G then C");
  await expect(dialog.locator('[data-shortcut="palette"]')).toContainText(/(Command|Control) K/);
  await expect(dialog.locator('[data-shortcut="dispatch"]')).toContainText("On a project's pages, with the Writer role");
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousViolations(page, `the shortcuts dialog in ${scheme}`);
  }
  await page.emulateMedia({ colorScheme: "light" });

  // Off: the single keys are marked so, do nothing, and the page shows none of them.
  const single = dialog.getByRole("switch", { name: "Single-key shortcuts" });
  await expect(single).toBeChecked();
  await single.click();
  await expect(single).not.toBeChecked();
  await expect(dialog.locator('[data-shortcut="search"]')).toContainText("Off");
  await expect(dialog.locator('[data-shortcut="palette"]')).not.toContainText("Off");
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  await page.reload();
  await expect(main(page).getByTestId("runs-table")).toBeVisible();
  await hydrated(page);
  await expect(main(page).getByTestId("runs-dispatch").locator('[data-shortcut="dispatch"]')).toHaveCount(0);
  await expect(main(page).getByTestId("runs-search")).not.toHaveAttribute("aria-keyshortcuts", "/");
  await page.keyboard.press("/");
  await expect(main(page).getByTestId("runs-search")).not.toBeFocused();
  await goTo(page, "i");
  await page.keyboard.press("d");
  await page.keyboard.press("Shift+Slash");
  await page.waitForTimeout(300);
  expect(page.url()).toBe(url);
  await expect(page.getByRole("dialog")).toHaveCount(0);

  // Cmd K or Ctrl K still opens the palette, which offers the shortcuts dialog when asked.
  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByTestId("command-palette");
  await expect(palette).toBeVisible();
  await page.keyboard.type("keyboard");
  await expect(palette.locator('[data-item="help:shortcuts"]')).toHaveAttribute("aria-selected", "true");
  await page.keyboard.press("Escape");
  await expect(palette).toBeHidden();

  // The account menu opens the dialog too, and focus comes back to the menu's button.
  const account = page.getByTestId("user-menu");
  await account.click();
  await page.getByTestId("user-menu-shortcuts").click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("switch", { name: "Single-key shortcuts" }).click();
  await expect(dialog.getByRole("switch", { name: "Single-key shortcuts" })).toBeChecked();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(account).toBeFocused();

  // On again: G I goes to the Inbox.
  await goTo(page, "i");
  await expect(page).toHaveURL(/\/inbox$/);
});
