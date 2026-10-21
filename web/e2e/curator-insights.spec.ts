import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { seedCurator } from "./support/curator";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";

/**
 * The Curator's History, Graph health and Runtime pages against the real API, once the night's figures are counted
 * (e2e/support/curator.ts plays the hub's job and the worker on duty): each opens from the Curator's tabs, names itself
 * in the trail, and shows what the stack holds of the project, the empty or hidden state where it holds nothing (no
 * graph on the stack's hub, a new project's transcripts); the history's search keeps its words in the URL and says
 * when nothing matches; the charter form keeps the replays and the history run as written. axe checks each page.
 */
test.skip(isDeployed, "writes charters and plays the night's worker through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("the History page says how far the hub holds the history, and searches it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedCurator(me, project, uniqueName("history"));

  await open(page, `/p/${project}/curator/history`);
  await expect(main(page).getByTestId("history-page")).toBeVisible();
  await expect(main(page).getByTestId("curator-tab-history")).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("navigation", { name: "Current location" })).toContainText("History");
  await expect(main(page).getByTestId("history-coverage")).toBeVisible();
  await expect(main(page).getByTestId("history-members")).toBeVisible();
  await expect(main(page).getByTestId("history-runs")).toBeVisible();
  await expect(main(page).getByTestId("history-clusters")).toBeVisible();
  await expect(main(page).getByTestId("history-sessions")).toBeVisible();
  await expect(main(page).getByTestId("history-search-idle")).toBeVisible();
  await expectNoSeriousViolations(page, "the Curator's history");

  const field = main(page).getByTestId("history-search-field");
  await field.fill("zzqx-nothing-matches");
  await field.press("Enter");
  await expect(page).toHaveURL(/[?&]q=zzqx-nothing-matches/);
  const none = main(page).getByTestId("history-search-none");
  await expect(none).toBeVisible();
  await expect(none.getByTestId("filters-in-use")).toContainText("zzqx-nothing-matches");
  await expectNoSeriousViolations(page, "a search of the history that matches nothing");
  await none.getByTestId("clear-filters").click();
  await expect(main(page).getByTestId("history-search-idle")).toBeVisible();
  await expect(field).toHaveValue("");

  // The sessions' filters live in the URL too, and a filter nothing matches offers to clear it.
  await main(page).getByTestId("history-sessions-outcome").selectOption("abandoned");
  await expect(page).toHaveURL(/[?&]outcome=abandoned/);
});

test("the Graph health and Runtime pages open from the Curator's tabs", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedCurator(me, project, uniqueName("graph"));

  await open(page, `/p/${project}/curator`);
  await main(page).getByTestId("curator-tab-graph").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/curator/graph$`));
  await expect(main(page).getByTestId("graph-page")).toBeVisible();
  // The stack's hub builds no graph of this project: the page says what it measures, or that its label is not the
  // visitor's to read.
  await expect(main(page).getByTestId("graph-empty").or(main(page).getByTestId("graph-hidden"))).toBeVisible();
  await expectNoSeriousViolations(page, "the graph's health");

  await main(page).getByTestId("curator-tab-runtime").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/curator/runtime$`));
  await expect(main(page).getByTestId("runtime-page")).toBeVisible();
  await expect(main(page).getByTestId("runtime-strip").or(main(page).getByTestId("runtime-empty"))).toBeVisible();
  await expectNoSeriousViolations(page, "the runtime");

  await main(page).getByTestId("runtime-range-7").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/curator/runtime\\?nights=7$`));
  await expect(main(page).getByTestId("runtime-range-7")).toHaveAttribute("aria-pressed", "true");
});

test("the charter form keeps the replays and the history run, and refuses a replay cap over the night's", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedCurator(me, project, uniqueName("charter"));

  await open(page, `/p/${project}/curator/charter?edit=1`);
  const form = main(page).getByTestId("charter-form");
  await expect(form.getByTestId("charter-night-runs")).toBeVisible();
  await form.getByTestId("charter-replay-tasks").fill("6");
  await form.getByTestId("charter-replay-budget").fill("9999");
  await form.getByTestId("charter-save").click();
  await expect(form.getByTestId("charter-error-replayBudget")).toBeVisible();
  await expect(form.getByTestId("charter-replay-budget")).toBeFocused();

  await form.getByTestId("charter-replay-budget").fill("0.5");
  await form.getByTestId("charter-history-budget").fill("0");
  await form.getByTestId("charter-save").click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/curator/charter$`));
  const view = main(page).getByTestId("charter-view-night-runs");
  await expect(view.getByTestId("charter-replay-tasks-value")).toHaveText("6");
  await expect(view.getByTestId("charter-replay-budget-value")).toHaveText("$0.50");
  await expect(view.getByTestId("charter-history-budget-value")).toHaveText("None: no history run");
});
