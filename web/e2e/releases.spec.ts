import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, test } from "./support/fixtures";
import { HubAdmin, newAccount } from "./support/hub";
import { open } from "./support/plans";
import { RED_TEST, seedRelease, setSchedule, WORKFLOW_URL } from "./support/releases";
import { toast } from "./support/toast";

/**
 * A project's Releases page against the real API (e2e/support/releases.ts sets the schedule, and the stack plays the
 * release job the hub cannot run here without a GitHub App): the schedule, the newest checks, the cycle open now with
 * its red tag, its workflow run, the test that failed and its cut and fix runs linked to their pages, its cost against
 * its cap, and the cycles that ended. An admin pauses the schedule (confirmed: it cancels the queued fix run and closes
 * the cycle left idle), resumes it, and runs the check now, which says why it failed. A stopped cycle says why and
 * links its decision and its triage thread. A reader reads everything and changes nothing; a project without a
 * schedule says what one does and how an admin sets it. No pill says the page is live or the schedule fine; the page
 * fits a phone without scrolling sideways; axe checks it in light and dark.
 */
test.skip(isDeployed, "seeds release cycles through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

async function sideways(page: Page): Promise<number> {
  return page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
}

test("an admin reads the cycle and the ones before it, pauses and resumes the schedule, and runs a check now", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const seeded = await seedRelease(me, project);

  await open(page, `/p/${project}/releases`);
  await expect(main(page).getByRole("heading", { level: 1, name: "Releases" })).toBeVisible();
  await expect(page.getByTestId("nav-releases")).toHaveAttribute("aria-current", "page");
  const header = main(page).getByTestId("release-header");
  await expect(header.getByTestId("release-state")).toHaveCount(0); // nothing to say while the schedule runs as it should
  await expect(header).toContainText(`Checks example-org/api every day at 06:00 Asia/Ho_Chi_Minh, as ${me.login}. Next check in`);

  // The cycle open now: its red tag with the workflow run and the test that failed, its runs, its cost.
  const cycle = main(page).getByTestId("release-cycle");
  await expect(cycle.getByRole("heading", { level: 2 })).toHaveText(`Cycle #${seeded.cycle}`);
  await expect(cycle.getByTestId("release-cycle-state")).toHaveCount(0);
  const tags = cycle.getByTestId("release-tags");
  await expect(tags.getByTestId("release-tag")).toHaveText("v0.25.0");
  await expect(tags.getByTestId("release-ci")).toHaveText("Failed");
  await expect(tags.getByTestId("release-workflow-link")).toHaveAttribute("href", WORKFLOW_URL);
  await expect(tags.getByTestId("release-failed-tests")).toHaveText(RED_TEST);
  const runs = cycle.getByTestId("release-runs");
  await expect(runs.getByTestId("release-run-link")).toHaveText([`#${seeded.cut}`, `#${seeded.fix}`]);
  await expect(runs.getByTestId("release-run-mode")).toHaveText(["Cut", "Fix of v0.25.0"]);
  await expect(runs.getByTestId("run-state")).toHaveText(["Done", "Queued"]);
  await expect(runs).toContainText("Tagged v0.25.0");
  await expect(cycle.getByTestId("release-cycle-cost")).toContainText("$8.15 of $200.00, 4%");

  // The schedule, the newest check and those before it, and the cycles that ended.
  await expect(main(page).getByTestId("release-cap")).toHaveText("$200.00");
  await expect(main(page).getByTestId("release-owner")).toHaveText(me.login);
  await expect(main(page).getByTestId("release-last-outcome")).toHaveText(`Opened cycle #${seeded.cycle}`);
  await expect(main(page).getByTestId("release-last-trigger")).toHaveText(/^The daily check of /);
  await expect(main(page).getByTestId("release-checks").locator("tbody tr")).toHaveCount(1); // the one before the last
  await expect(main(page).getByTestId("release-checks")).toContainText("Nothing to release");
  const past = main(page).getByTestId("release-past");
  await expect(past.locator("tbody tr")).toHaveCount(2);
  await expect(past.locator("tbody tr").first()).toContainText(`#${seeded.closed}`);
  await expect(past.locator("tbody tr").first()).toContainText("Closed: the release schedule of");
  await expect(past.locator("tbody tr").nth(1)).toContainText("Published v0.24.0");
  await expectNoSeriousViolations(page, "the Releases page");

  // A run of the cycle opens on its own page.
  await runs.getByTestId("release-run-link").first().click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/runs/${seeded.cut}$`));
  await expect(main(page).getByTestId("run-detail")).toHaveAttribute("data-kind", "release");
  await page.goBack();

  // A pause is confirmed: it cancels the queued fix run, and the cycle left with nothing under way closes.
  await main(page).getByTestId("release-pause").click();
  const dialog = page.getByTestId("release-pause-dialog");
  await expect(dialog).toContainText(`No daily check runs for ${project} until an admin resumes it`);
  await expectNoSeriousViolations(page, "the pause dialog");
  await dialog.getByTestId("release-pause-dialog-confirm").click();
  await expect(toast(page, "Release schedule paused")).toBeVisible();
  await expect(header.getByTestId("release-state")).toHaveText("Paused");
  await expect(header).toContainText(`Paused by ${me.login}`);
  await expect(main(page).getByTestId("release-run-now")).toHaveCount(0);
  await expect(main(page).getByTestId("release-no-cycle")).toContainText("The schedule is paused");
  await expect(past.locator("tbody tr")).toHaveCount(3);
  await expect(past.locator("tbody tr").first()).toContainText(`#${seeded.cycle}`);
  await expect(past.locator("tbody tr").first()).toContainText(`was paused by ${me.login}`);

  await main(page).getByTestId("release-resume").click();
  await expect(toast(page, "Release schedule resumed")).toBeVisible();
  await expect(header.getByTestId("release-state")).toHaveCount(0);

  // Run now goes through the daily check's gates; this hub has no GitHub App, so the check says it read no tag.
  await main(page).getByTestId("release-run-now").click();
  await expect(toast(page, "The check failed")).toBeVisible();
  await expect(main(page).getByTestId("release-last-outcome")).toHaveText("Failed");
  await expect(main(page).getByTestId("release-last-reason")).toContainText("this hub has no GitHub App");
  await expect(main(page).getByTestId("release-last-trigger")).toHaveText(`Run now by ${me.login}`);
});

test("a stopped cycle says why, waits for its owner and links the decision and the triage thread", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const seeded = await seedRelease(me, project, { stopped: true });

  await open(page, `/p/${project}/releases`);
  const banner = main(page).getByTestId("release-stopped");
  await expect(banner).toContainText(`Cycle #${seeded.cycle} stopped: the same test failed at two tags in a row`);
  await expect(banner).toContainText(RED_TEST);
  await expect(banner).toContainText(`It moves no further until ${me.login} answers`);
  await expect(banner.getByTestId("release-decision-link")).toHaveAttribute("href", /^\/inbox\?decision=\d+$/);
  await expect(banner.getByTestId("release-thread-link")).toHaveAttribute("href", /^\/chats\/\d+\?message=\d+$/);
  await expect(main(page).getByTestId("release-cycle").getByTestId("release-cycle-state")).toHaveText("Stopped");
  await expectNoSeriousViolations(page, "a stopped cycle");

  await banner.getByTestId("release-decision-link").click();
  const sheet = page.getByTestId("decision-sheet");
  await expect(sheet).toContainText(`Release cycle #${seeded.cycle} of ${project} stopped`);
  await expect(sheet.getByTestId("decision-category")).toHaveText("Release");
});

test("a reader reads the release and changes nothing", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  const owner = newAccount("owner");
  await new HubAdmin().grant(project, owner.login, "admin", "internal");
  const seeded = await seedRelease(owner, project);

  await open(page, `/p/${project}/releases`);
  await expect(main(page).getByTestId("release-cycle").getByRole("heading", { level: 2 })).toHaveText(`Cycle #${seeded.cycle}`);
  await expect(main(page).getByTestId("release-owner")).toHaveText(owner.login);
  await expect(main(page).getByTestId("release-past").locator("tbody tr")).toHaveCount(2);
  for (const control of ["release-run-now", "release-pause", "release-resume"]) {
    await expect(main(page).getByTestId(control)).toHaveCount(0);
  }
});

test("a project without a schedule says what the daily release does and how an admin sets it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  await open(page, `/p/${project}/releases`);
  await expect(main(page).getByTestId("state-empty")).toContainText("No release schedule yet");
  await expect(main(page).getByTestId("release-set-command")).toHaveText(`evo-agents hub release set --project ${project}`);
  await expect(main(page).getByTestId("release-run-now")).toHaveCount(0);
  await expectNoSeriousViolations(page, "Releases without a schedule");

  // Once set, the schedule shows and nothing has been checked yet.
  await setSchedule(me, project);
  await page.reload();
  await expect(main(page).getByTestId("release-last-check-none")).toContainText("No check yet. The first runs at 06:00 Asia/Ho_Chi_Minh");
  await expect(main(page).getByTestId("release-no-cycle")).toContainText("No cycle open. The next check is in");
  await expect(main(page).getByTestId("release-past-none")).toHaveText("No cycle has ended yet.");
});

test.describe("dark theme", () => {
  test.use({ colorScheme: "dark" });

  test("the page and a stopped cycle have no serious axe violation", async ({ page, member }) => {
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    await seedRelease(me, project, { stopped: true });
    await open(page, `/p/${project}/releases`);
    await expect(main(page).getByTestId("release-stopped")).toBeVisible();
    await expect(page.locator("html")).toHaveClass(/dark/);
    await expectNoSeriousViolations(page, "the Releases page (dark)");
  });
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true });

  test("the page lists its tables as rows and never scrolls sideways", async ({ page, member }) => {
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    const seeded = await seedRelease(me, project);
    await open(page, `/p/${project}/releases`);
    await expect(main(page).getByTestId("release-runs")).toHaveAttribute("data-layout", "list");
    await expect(main(page).getByTestId("release-past")).toHaveAttribute("data-layout", "list");
    await expect(main(page).getByTestId("release-runs").locator(`[data-run-id="${seeded.fix}"]`)).toBeVisible();
    await expect(main(page).getByTestId("release-tag-details")).toContainText(RED_TEST); // what failed follows the list
    await expect(main(page).getByTestId("release-pause")).toBeVisible();
    expect(await sideways(page), "the Releases page at 375 px scrolls sideways").toBeLessThanOrEqual(0);
    await expectNoSeriousViolations(page, "the Releases page on a phone");
  });
});
