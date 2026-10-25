import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { localAsk, localEvents, localState, registerLocal } from "./support/local";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, RUN_PLAN, runPath, runsOf, say, seedRunPlan, startRun } from "./support/runs";
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

test("the runs list and a local session's page follow the session live, and the list filters by kind and account", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await dispatch(me, project, ["2"]);
  const { run } = await registerLocal(me, project, "7c1d2e3f-0000-4000-8000-000000000d01", { machine: "laptop-binh", email: "binh@work.example", title: "Fix the queue" });
  const other = await registerLocal(me, project, "7c1d2e3f-0000-4000-8000-000000000d02", { machine: "desktop-home", email: "binh@home.example", title: "Tidy the docs" });
  await localState(me, project, run.id, { state: "running", plan_id: RUN_PLAN, step_key: "2" });

  await open(page, `/p/${project}/runs`);
  const main = page.locator("#main");
  const row = main.getByTestId("runs-table").locator("tbody tr").filter({ has: page.locator(`[data-run-id="${run.id}"]`) });
  await expect(row.getByTestId("run-kind")).toHaveText("Local session");
  await expect(row.getByTestId("run-where")).toHaveText(`laptop-binh, binh@work.example, plan ${RUN_PLAN}, step 2`);
  await expect(row.getByTestId("run-state")).toHaveText("Running");

  // The state changes reach the list without a reload: an idle session leaves Running for the Idle tab, and comes
  // back when it runs again.
  await localState(me, project, run.id, { state: "idle" });
  await expect(row).toHaveCount(0, { timeout: 15_000 });
  await main.getByTestId("runs-facets").getByRole("button", { name: /^Idle/ }).click();
  await expect(row.getByTestId("run-state")).toHaveText("Idle");
  await localState(me, project, run.id, { state: "running" });
  await expect(row).toHaveCount(0, { timeout: 15_000 });
  await main.getByTestId("runs-facets").getByRole("button", { name: /^All/ }).click();
  await expect(row.getByTestId("run-state")).toHaveText("Running");

  // Filter by kind, then by account.
  await main.getByTestId("runs-kinds").getByRole("button", { name: /Local sessions/ }).click();
  await expect(main.getByTestId("runs-table").locator("tbody tr")).toHaveCount(2);
  await main.getByTestId("runs-account").fill("binh@home.example");
  await expect(main.getByTestId("runs-table").locator("tbody tr")).toHaveCount(1);
  await expect(page).toHaveURL(/kind=local&account=binh%40home\.example/);

  // The run's page follows the session's stream, and the session ending shows as Ended.
  await open(page, runPath(project, other.run.id));
  await expect(main.getByTestId("run-detail")).toHaveAttribute("data-state", "running");
  await localEvents(me, project, other.run.id, [say("Tidying the docs.")]);
  await expect(main.getByTestId("trace").getByTestId("trace-message")).toHaveText("Tidying the docs.");
  await localAsk(me, project, other.run.id, "Rename the guide?");
  await localState(me, project, other.run.id, { state: "ended" });
  await expect(main.getByTestId("run-detail")).toHaveAttribute("data-state", "ended", { timeout: 15_000 });
  await expect(main.getByTestId("run-status-line")).toContainText("Ended after");
});
