import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, runPath, runsOf, seedRunPlan, startRun } from "./support/runs";
import { toast } from "./support/toast";

/**
 * Whether a page is current, and what a write did, against the real API: the top bar's LiveIndicator follows the runs
 * page's polling (Live, then Reconnecting and Offline while the browser cannot reach /v1, then Live again), a run's
 * event stream and the log's Pause (Paused, and Resume from the top bar), and Dispatch reports the run it queued in a
 * toast that links to it. Pages render in English.
 */
test.skip(isDeployed, "blocks the hub API and dispatches runs on the local stack");

const API = "**/v1/**";

function indicator(page: Page) {
  return page.getByTestId("top-bar").getByTestId("live-indicator");
}

test("the top bar says Live, then Offline while the hub cannot be reached, then Live again", async ({ page, member }) => {
  test.setTimeout(90_000);
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await dispatch(me, project, ["2"]); // an active run: the page reads every 5 seconds
  await open(page, `/p/${project}/runs`);

  const live = indicator(page);
  await expect(live).toHaveAttribute("data-state", "live");
  await expect(live.getByRole("status")).toHaveText("Updates: Live");
  await expect(live.getByTestId("live-detail")).toHaveText(/^updated (just now|\d+s ago)$/);

  // The network drops: every read of the hub fails from now on.
  await page.route(API, (route) => route.abort("internetdisconnected"));
  await expect(live).toHaveAttribute("data-state", "reconnecting", { timeout: 15_000 });
  await expect(live.getByRole("status")).toHaveText("Updates: Reconnecting");
  await expect(live.getByTestId("live-detail")).toContainText("polling every 5s");

  // Failing for 15 seconds: Offline, with the time of the last update and Retry now. The page keeps what it showed.
  await expect(live).toHaveAttribute("data-state", "offline", { timeout: 30_000 });
  await expect(live.getByRole("status")).toHaveText("Updates: Offline");
  await expect(live.getByTestId("live-detail")).toHaveText(/^last update \d+s ago$/);
  await expect(page.locator("#main").getByTestId("runs-summary")).toBeVisible();

  // The network is back: Retry now reads at once, and the page is live again.
  await page.unroute(API);
  await live.getByTestId("live-retry").click();
  await expect(live).toHaveAttribute("data-state", "live");
  await expect(live.getByTestId("live-retry")).toHaveCount(0);
});

test("a run's page follows its event stream, and the log's Pause shows as Paused until Resume", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const worker = await liveWorker(me, project, uniqueName("laptop"));
  const [run] = await dispatch(me, project, ["2"]);
  expect((await claimRun(worker))?.id).toBe(run.id);
  await startRun(worker, run.id);

  await open(page, `${runPath(project, run.id)}?view=log`);
  const main = page.locator("#main");
  await expect(main.getByTestId("log-status")).toHaveAttribute("data-status", "live");
  const live = indicator(page);
  await expect(live).toHaveAttribute("data-state", "live");
  await expect(live).toHaveAttribute("data-transport", "stream");

  await main.getByTestId("log-pause").click();
  await expect(live).toHaveAttribute("data-state", "paused");
  await expect(live.getByRole("status")).toHaveText("Updates: Paused");
  await expect(live.getByTestId("live-detail")).toHaveText("by you");

  await live.getByTestId("live-resume").click();
  await expect(main.getByTestId("log-pause")).toHaveAttribute("aria-pressed", "false");
  await expect(live).toHaveAttribute("data-state", "live");
});

test("Dispatch says in a toast which run it queued, with a link to it", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await liveWorker(me, project, uniqueName("desk"));

  await open(page, `/p/${project}/runs`);
  await page.locator("#main").getByTestId("runs-empty-dispatch").click();
  const dialog = page.getByTestId("dispatch-dialog");
  await dialog.getByTestId("dispatch-step-2").click();
  await dialog.getByTestId("dispatch-submit").click();
  await expect(dialog).toBeHidden();

  const [run] = await runsOf(me, project);
  const done = toast(page, `Run #${run.id} dispatched`);
  await expect(done).toBeVisible();
  await expect(done).toHaveAttribute("data-tone", "success");
  await expect(done).toContainText("It waits in the queue for a worker.");
  // Toasts sit in Sonner's polite live region, bottom right.
  await expect(page.getByRole("region", { name: /^Notifications/ })).toHaveAttribute("aria-live", "polite");
  const box = await done.boundingBox();
  const viewport = page.viewportSize();
  expect(box && viewport ? box.x + box.width > viewport.width / 2 && box.y + box.height > viewport.height / 2 : false).toBe(true);

  await done.getByRole("link", { name: "Open run" }).click();
  await page.waitForURL(`**${runPath(project, run.id)}`);
  await expect(page.locator("#main").getByRole("heading", { level: 1 })).toContainText(`Run #${run.id}`);
});
