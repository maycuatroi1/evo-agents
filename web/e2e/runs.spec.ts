import type { Page } from "@playwright/test";

import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import {
  claimRun,
  dispatch,
  liveWorker,
  reportState,
  RUN_PLAN,
  runRow,
  runsOf,
  seedRunPlan,
  STEP_TITLES,
  WAITING_STEP,
  workerHeartbeat,
} from "./support/runs";
import { toast } from "./support/toast";

/**
 * The Runs pages against the real API: the sidebar entry and the empty project; the Dispatch dialog offering only
 * ready steps (the others listed with their reason), saying which worker matches, and queueing runs the API then
 * holds as queued; the list following a worker's claim within one 5-second refresh; its facets and search; the plan
 * step page's Run this step button and runs; and a worker's page with the runs it took. Pages render in English.
 */
test.skip(isDeployed, "dispatches runs through the local stack");

function main(page: Page) {
  return page.locator("#main");
}

test("the sidebar leads to Runs, which offers a writer to dispatch the first run", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await page.goto(`/p/${project}`);
  await page.getByTestId("nav-runs").click();
  await page.waitForURL(`**/p/${project}/runs`);
  await expect(main(page).getByRole("heading", { level: 1, name: "Runs" })).toBeVisible();
  await expect(page.getByRole("navigation", { name: "Current location" })).toContainText("Runs");
  await expect(page.getByTestId("nav-runs")).toHaveAttribute("aria-current", "page");
  // The Monitor, every run in flight of every project, is one click away.
  await expect(main(page).getByRole("link", { name: "Monitor" })).toHaveAttribute("href", "/monitor");
  const empty = main(page).getByTestId("state-empty");
  await expect(empty).toContainText("No run yet");
  await empty.getByTestId("runs-empty-dispatch").click();
  const dialog = page.getByTestId("dispatch-dialog");
  await expect(dialog.getByRole("heading", { name: "Dispatch runs" })).toBeVisible();
  await expect(dialog.getByTestId("dispatch-no-plans")).toContainText("no active plan");
  await expect(dialog.getByTestId("dispatch-no-workers")).toContainText(`no worker registered for project ${project}`);
  await expect(dialog.getByTestId("dispatch-submit")).toBeDisabled();
});

test("dispatch queues runs of the ready steps only, and the list follows a worker's claim live", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"));

  await open(page, `/p/${project}/runs`);
  await main(page).getByTestId("runs-empty-dispatch").click();
  const dialog = page.getByTestId("dispatch-dialog");
  await expect(dialog.getByTestId("dispatch-plan")).toHaveValue(RUN_PLAN);

  // A ready step can be picked; a step that waits for another is listed with the reason and cannot be.
  await expect(dialog.getByTestId("dispatch-step-2").getByRole("checkbox")).toBeEnabled();
  const waiting = dialog.getByTestId(`dispatch-step-${WAITING_STEP}`);
  await expect(waiting).toContainText(STEP_TITLES[WAITING_STEP]);
  await expect(waiting).toContainText("It waits for step 2 (pending)");
  await expect(waiting.getByRole("checkbox")).toBeDisabled();
  await waiting.click({ force: true }); // clicking the card does nothing
  await expect(waiting.getByRole("checkbox")).not.toBeChecked();
  // Steps that are done or in progress are folded away, and cannot be picked either.
  await expect(dialog.getByTestId("dispatch-settled")).toContainText("2 steps are done, in progress or blocked");
  await dialog.getByTestId("dispatch-settled").locator("summary").click();
  await expect(dialog.getByTestId("dispatch-step-1")).toContainText("Its status is Done, not pending");
  await expect(dialog.getByTestId("dispatch-step-1").getByRole("checkbox")).toBeDisabled();
  await expect(dialog.getByTestId("dispatch-step-5").getByRole("checkbox")).toBeDisabled();

  await expect(dialog.getByTestId("dispatch-submit")).toBeDisabled();
  await expect(dialog.getByTestId("dispatch-outlook")).toHaveText("Pick at least one ready step.");
  await dialog.getByTestId("dispatch-step-2").click();
  await dialog.getByTestId("dispatch-step-4").click();
  await expect(dialog.getByTestId("dispatch-picked")).toHaveText("2 of 2 ready steps picked");
  await expect(dialog.getByTestId("dispatch-outlook")).toHaveText(`Matching now: ${live.worker.name} (1 free slot).`);
  await dialog.getByRole("radio", { name: /^Codex CLI/ }).check();
  await expect(dialog.getByTestId("dispatch-outlook")).toContainText(`${live.worker.name}: no Codex CLI`);
  await dialog.getByRole("radio", { name: /^Claude Code/ }).check();
  await expect(dialog.getByRole("radio", { name: /^Hold for review/ })).toBeChecked();
  await expect(dialog.getByTestId("dispatch-submit")).toHaveText("Dispatch 2 runs");
  await dialog.getByTestId("dispatch-submit").click();
  await expect(dialog).toBeHidden();

  // The API holds two queued runs, one per step, as asked.
  const runs = await runsOf(me, project);
  expect(runs.map((run) => run.step_key).sort()).toEqual(["2", "4"]);
  for (const run of runs) {
    expect(run).toMatchObject({
      state: "queued",
      plan_id: RUN_PLAN,
      requested_runtime: "claude-code",
      mode: "headless",
      approval: "review",
      timeout_min: 60,
      dispatched_by: me.login,
      pinned_worker_id: null,
    });
  }
  const [newer, older] = runs;
  await expect(toast(page, `2 runs dispatched: #${older.id} and #${newer.id}`)).toContainText("They wait in the queue for a worker.");
  await expect(runRow(page, older.id).getByTestId("run-state")).toHaveText("Queued");
  await expect(runRow(page, newer.id).getByTestId("run-state")).toHaveText("Queued");
  await expect(runRow(page, older.id)).toContainText(STEP_TITLES["2"]);
  await expect(main(page).getByTestId("summary-queued").locator("dd").first()).toHaveText("2");
  // Active runs: the page reads every 5 seconds, and the top bar says it is live.
  await expect(page.getByTestId("live-indicator")).toHaveAttribute("data-state", "live");

  // The worker claims the older run: the list shows it leased within one refresh, without a reload.
  const claimed = await claimRun(live);
  expect(claimed?.id).toBe(older.id);
  await expect(runRow(page, older.id).getByTestId("run-state")).toHaveText("Leased", { timeout: 12_000 });
  await expect(runRow(page, older.id)).toContainText(live.worker.name);
  await expect(main(page).getByTestId("summary-running").locator("dd").first()).toHaveText("1");
  await expect(main(page).getByTestId("summary-running")).toContainText("on 1 worker");

  // Dispatched steps now have an active run: the dialog lists them with it and they cannot be picked again.
  await main(page).getByTestId("runs-dispatch").click();
  await expect(dialog.getByTestId("dispatch-step-2")).toContainText(`Run #${older.id} is Leased, dispatched by ${me.login}`);
  await expect(dialog.getByTestId("dispatch-step-4")).toContainText(`Run #${newer.id} is Queued`);
  await expect(dialog.getByTestId("dispatch-step-4").getByRole("checkbox")).toBeDisabled();
  await expect(dialog.getByTestId("dispatch-none-ready")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();

  // The worker's page lists the run it took.
  await workerHeartbeat(live, [older.id]);
  await open(page, `/workers/${live.worker.id}`);
  await expect(runRow(page, older.id, "worker-runs-table").getByTestId("run-state")).toHaveText("Leased");
  await expect(runRow(page, older.id, "worker-runs-table")).toContainText(`${project}, ${RUN_PLAN}, step 2`);
});

test("the list filters by state and search, in the URL", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("desk"));
  const [failing, waiting] = await dispatch(me, project, ["2", "4"]);
  expect((await claimRun(live))?.id).toBe(failing.id);
  expect(await reportState(live, failing.id, { state: "failed", error: "verify failed: pnpm test exited 1" })).toMatchObject({
    state: "failed",
  });

  await open(page, `/p/${project}/runs`);
  const facets = main(page).getByTestId("runs-facets");
  await expect(facets.getByRole("button", { name: /^All/ })).toHaveAttribute("aria-pressed", "true");
  await expect(main(page).getByTestId("summary-failed").locator("dd").first()).toHaveText("1");
  await expect(runRow(page, failing.id).getByTestId("run-state")).toHaveText("Failed");
  await expect(runRow(page, failing.id).getByTestId("run-error")).toHaveText("verify failed: pnpm test exited 1");

  await facets.getByRole("button", { name: /^Failed or cancelled/ }).click();
  await expect(page).toHaveURL(/\?state=ended$/);
  await expect(main(page).getByTestId("runs-table").locator("tbody tr")).toHaveCount(1);
  await expect(runRow(page, failing.id)).toBeVisible();
  await facets.getByRole("button", { name: /^Active/ }).click();
  await expect(main(page).getByTestId("runs-table").locator("tbody tr")).toHaveCount(1);
  await expect(runRow(page, waiting.id)).toBeVisible();
  await facets.getByRole("button", { name: /^Done/ }).click();
  const none = main(page).getByTestId("state-empty");
  await expect(none).toContainText("No run matches");
  await expect(none.getByTestId("filters-in-use")).toHaveText("State:Done"); // the filter in force, named
  await none.getByRole("button", { name: "Clear filters" }).click();
  await expect(main(page).getByTestId("runs-table").locator("tbody tr")).toHaveCount(2);

  // "/" focuses the search from anywhere on the page but a text field.
  await page.keyboard.press("/");
  await expect(main(page).getByTestId("runs-search")).toBeFocused();
  await expect(main(page).getByTestId("runs-search")).toHaveValue("");
  await main(page).getByTestId("runs-search").fill(STEP_TITLES["4"]);
  await expect(page).toHaveURL(new RegExp(`\\?q=${encodeURIComponent(STEP_TITLES["4"]).replace(/%20/g, "\\+")}$`));
  await expect(main(page).getByTestId("runs-table").locator("tbody tr")).toHaveCount(1);
  await expect(runRow(page, waiting.id)).toBeVisible();
  await main(page).getByTestId("runs-search").fill(`#${failing.id}`);
  await expect(main(page).getByTestId("runs-table").locator("tbody tr")).toHaveCount(1);
  await expect(runRow(page, failing.id)).toBeVisible();
  await expect(main(page).getByTestId("runs-list-summary")).toHaveText("1 run matches");

  // The filters survive a reload.
  await page.reload();
  await expect(main(page).getByTestId("runs-search")).toHaveValue(`#${failing.id}`);
  await expect(main(page).getByTestId("runs-table").locator("tbody tr")).toHaveCount(1);
});

test("a plan step's page runs the step, and lists its runs", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("mini"));

  // A step that is not ready says why, and its button does nothing.
  await open(page, `/p/${project}/plans/${RUN_PLAN}/steps/${WAITING_STEP}`);
  const section = main(page).getByTestId("step-runs");
  await expect(section.getByTestId("step-readiness")).toHaveText("Not ready to run: It waits for step 2 (pending).");
  await expect(section.getByTestId("step-run")).toHaveAttribute("aria-disabled", "true");
  await section.getByTestId("step-run").click({ force: true });
  await expect(page.getByTestId("dispatch-dialog")).toHaveCount(0);
  await expect(section.getByTestId("step-runs-empty")).toBeVisible();

  // A ready step opens the dialog with itself picked.
  await open(page, `/p/${project}/plans/${RUN_PLAN}/steps/4`);
  await expect(section.getByTestId("step-readiness")).toHaveText("Ready to run.");
  await section.getByTestId("step-run").click();
  const dialog = page.getByTestId("dispatch-dialog");
  await expect(dialog.getByTestId("dispatch-step-4").getByRole("checkbox")).toBeChecked();
  await expect(dialog.getByTestId("dispatch-step-2").getByRole("checkbox")).not.toBeChecked();
  await dialog.getByTestId("dispatch-target-pin").click();
  await expect(dialog.getByTestId("dispatch-pinned-worker")).toHaveValue(String(live.worker.id));
  await expect(dialog.getByTestId("dispatch-outlook")).toHaveText(`${live.worker.name} can take them now.`);
  await dialog.getByRole("radio", { name: /^Mark the step done with evidence/ }).check();
  await dialog.getByTestId("dispatch-submit").click();
  await expect(dialog).toBeHidden();

  const [run] = await runsOf(me, project);
  expect(run).toMatchObject({ step_key: "4", state: "queued", pinned_worker_id: live.worker.id, approval: "auto" });
  await expect(toast(page, `Run #${run.id} dispatched`)).toBeVisible();
  await expect(runRow(page, run.id, "step-runs-table").getByTestId("run-state")).toHaveText("Queued");
  await expect(runRow(page, run.id, "step-runs-table")).toContainText(me.login);
  await expect(section.getByTestId("step-readiness")).toHaveText(`Not ready to run: Run #${run.id} is Queued, dispatched by ${me.login}.`);
  await expect(section.getByTestId("step-run")).toHaveAttribute("aria-disabled", "true");
});

test.describe("in Vietnamese", () => {
  test.use({ uiLocale: "vi" }); // the assertions below read the Vietnamese copy of messages/vi.json

  test("the runs page and the dialog keep the English terms", async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    const project = me.projects[0];
    await seedRunPlan(me, project);
    await open(page, `/p/${project}/runs`);
    await expect(main(page).getByRole("heading", { level: 1, name: "Run" })).toBeVisible();
    await main(page).getByTestId("runs-empty-dispatch").click();
    const dialog = page.getByTestId("dispatch-dialog");
    await expect(dialog.getByRole("heading", { name: "Dispatch run" })).toBeVisible();
    await expect(dialog.getByTestId(`dispatch-step-${WAITING_STEP}`)).toContainText("Chưa sẵn sàng");
    await expect(dialog.getByTestId("dispatch-outlook")).toHaveText("Chọn ít nhất một bước đã sẵn sàng.");
  });
});
