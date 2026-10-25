import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { marketingPath, seedGrowth } from "./support/growth";
import { HubAdmin, newAccount } from "./support/hub";
import { decidePath } from "./support/loop";
import { toast } from "./support/toast";

/**
 * Criterion a17 of plan autonomous-oss-growth for the core plugin marketing, in the browser against the real API: the
 * Marketing pages exist only where a project enabled marketing; the overview shows each channel's breaker and session,
 * the month's cost against its cap and the kill switch behind a confirmation; the posts are grouped by what the gate
 * did, a draft with its Copy button; the Growth weekly has its sections; the Decisions page lists the growth
 * decisions; a reader only reads.
 */
test.skip(isDeployed, "enables plugins and seeds channels and posts through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

/** The marketing page the hub frames in the main column (web/DESIGN.md, Plugins). */
function frame(page: Page) {
  return main(page).frameLocator('[data-testid="plugin-frame"]:not([data-panel])');
}

/** Marketing's panel of the Decisions page, the plugin's page of slot "decide" in a frame of its own. */
function panel(page: Page) {
  return main(page).frameLocator('[data-testid="plugin-frame"][data-panel]');
}

test("Marketing shows only where a project enabled it, then the overview has each channel's breaker and session and the month's cost", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await page.goto(marketingPath(project));
  await expect(page.getByTestId("state-not-found")).toBeVisible();
  await page.goto(`/p/${project}`);
  await expect(page.getByTestId("nav-plugin-marketing-overview")).toHaveCount(0);

  await seedGrowth(me, project);
  await page.goto(`/p/${project}/x/marketing`);
  await expect(page).toHaveURL(marketingPath(project));
  await expect(frame(page).getByTestId("marketing-channel-count")).toHaveText("4");
  await expect(frame(page).getByTestId("marketing-breaker-count")).toHaveText("1");
  await expect(frame(page).getByTestId("marketing-budget-figures")).toContainText("$12.50");
  await expect(frame(page).getByTestId("marketing-budget-figures")).toContainText("$50.00");
  await expect(frame(page).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");

  const rows = frame(page).getByTestId("marketing-channel-row");
  await expect(rows).toHaveCount(4);
  const x = rows.filter({ has: frame(page).getByTestId("marketing-breaker-open") });
  await expect(x).toHaveCount(1);
  await expect(x).toContainText("Two failures in a row");
  await expect(frame(page).getByTestId("marketing-session-stale")).toHaveCount(1);

  await x.getByTestId("marketing-close-breaker").click();
  await expect(toast(page, "Breaker of x acme closed")).toBeVisible();
  await expect(frame(page).getByTestId("marketing-breaker-open")).toHaveCount(0);
  await expect(frame(page).getByTestId("marketing-breaker-count")).toHaveText("0");
});

test("the kill switch stops all posting only after confirming, and an admin resumes it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await seedGrowth(me, project);
  await page.goto(marketingPath(project));

  await frame(page).getByTestId("marketing-kill").click();
  const dialog = frame(page).getByRole("alertdialog");
  await expect(dialog).toContainText("Stop all posting in this project?");
  await dialog.getByRole("button", { name: "Cancel" }).click();
  await expect(frame(page).getByTestId("marketing-killed")).toHaveCount(0);

  await frame(page).getByTestId("marketing-kill").click();
  await frame(page).getByTestId("marketing-kill-confirm-confirm").click();
  await expect(frame(page).getByTestId("marketing-killed")).toContainText("stopped from the web");
  await expect(frame(page).getByTestId("marketing-kill")).toHaveCount(0);

  await frame(page).getByTestId("marketing-unkill").click();
  await frame(page).getByTestId("marketing-kill-confirm-confirm").click();
  await expect(frame(page).getByTestId("marketing-killed")).toHaveCount(0);
  await expect(frame(page).getByTestId("marketing-kill")).toBeVisible();
});

test("the posts are grouped by what the gate did, and a draft for a Facebook group is copied", async ({ page, context, member }) => {
  await context.grantPermissions(["clipboard-read", "clipboard-write"]);
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await seedGrowth(me, project);
  await page.goto(marketingPath(project));
  await frame(page).getByTestId("marketing-tab-posts").click();
  await expect(page).toHaveURL(marketingPath(project, "posts"));

  const drafts = frame(page).getByTestId("marketing-group-drafts");
  await expect(drafts.getByTestId("marketing-post")).toHaveCount(1);
  await expect(drafts).toContainText("facebook_group");
  await expect(frame(page).getByTestId("marketing-group-waiting").getByTestId("marketing-post")).toHaveCount(1);
  await expect(frame(page).getByTestId("marketing-group-posted").getByTestId("marketing-post")).toHaveCount(1);
  await expect(frame(page).getByTestId("marketing-group-stopped").getByTestId("marketing-post-reason")).toContainText("deny-list");
  await expect(frame(page).getByTestId("marketing-copy")).toHaveCount(1);

  await drafts.getByTestId("marketing-copy").click();
  await expect(toast(page, "Draft copied to the clipboard")).toBeVisible();
  const copied = await page.evaluate(() => navigator.clipboard.readText());
  expect(copied).toContain("Chào cả nhóm");
});

test("the Growth weekly has the signal against each Goal, the posts, the breakers, the cost and the proposals", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await seedGrowth(me, project);
  await page.goto(marketingPath(project, "weekly"));
  for (const id of ["goals", "posted", "breakers", "cost", "proposals"]) {
    await expect(frame(page).getByTestId(`weekly-${id}`)).toBeVisible();
  }
  await expect(frame(page).getByTestId("weekly-post")).toHaveCount(1);
  await expect(frame(page).getByTestId("weekly-breaker")).toContainText("Two failures in a row");
  await expect(frame(page).getByTestId("weekly-week-cost")).toHaveText("$0.02");
  await expect(frame(page).getByTestId("weekly-cost").getByTestId("marketing-budget")).toBeVisible();
});

test("a reader reads the overview and the week, with no kill switch, and is told the posts are for writers", async ({ page, member }) => {
  const me = await member([{ role: "reader", maxLevel: "internal" }]);
  const [project] = me.projects;
  const owner = newAccount("marketing-owner");
  await new HubAdmin().grant(project, owner.login, "admin", "internal");
  await seedGrowth(owner, project);
  await page.goto(marketingPath(project));
  await expect(frame(page).getByTestId("marketing-read-only")).toBeVisible();
  await expect(frame(page).getByTestId("marketing-channel-row")).toHaveCount(4);
  await expect(frame(page).getByTestId("marketing-kill")).toHaveCount(0);
  await expect(frame(page).getByTestId("marketing-close-breaker")).toHaveCount(0);
  await page.goto(marketingPath(project, "posts"));
  await expect(frame(page).getByTestId("marketing-read-only")).toBeVisible();
  await expect(frame(page).getByTestId("marketing-post")).toHaveCount(0);
  await page.goto(marketingPath(project, "weekly"));
  await expect(frame(page).getByTestId("weekly-breaker")).toBeVisible();
});

test("the Decisions page lists the open breaker, the expired session and the spent budget, each opening Marketing", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const [project] = me.projects;
  await page.goto(decidePath(project));
  await expect(main(page).getByRole("heading", { level: 1 })).toBeVisible();
  await expect(main(page).getByTestId("plugin-frame")).toHaveCount(0);
  await expect(panel(page).getByTestId("decide-growth")).toHaveCount(0);

  await seedGrowth(me, project, { spent: 50 });
  await page.reload();
  const items = panel(page).getByTestId("growth-item");
  await expect(items).toHaveCount(3);
  await expect(items.nth(0)).toHaveAttribute("data-kind", "breaker");
  await expect(items.nth(0)).toContainText("x acme");
  await expect(items.nth(1)).toHaveAttribute("data-kind", "session");
  await expect(items.nth(1)).toContainText("linkedin acme-page");
  await expect(items.nth(2)).toHaveAttribute("data-kind", "budget");
  await items.nth(2).getByTestId("growth-item-open").click();
  await expect(page).toHaveURL(marketingPath(project));
  await expect(frame(page).getByTestId("marketing-budget")).toBeVisible();
});
