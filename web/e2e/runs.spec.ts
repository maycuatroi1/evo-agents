import type { Page } from "@playwright/test";

import { createAgentByApi } from "./support/agents";
import { chatSession, chatWorker } from "./support/chats";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { providerKey, putProviderByApi } from "./support/providers";
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
 * step page's Run this step button and runs; a chat session listed once, as its latest turn, opening into its turns;
 * and a worker's page with the runs it took. Pages render in English.
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

  await open(page, `/p/${project}/runs?state=all`); // the runs it dispatches are queued: the page opens on Running
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
  await expect(dialog.getByTestId("dispatch-outlook")).toHaveCount(0);
  await dialog.getByTestId("dispatch-step-2").click();
  await dialog.getByTestId("dispatch-step-4").click();
  await expect(dialog.getByTestId("dispatch-picked")).toHaveText("2 of 2 ready steps picked");
  // One free slot for two runs: the dropdown leaves them to any worker that fits.
  await expect(dialog.getByTestId("dispatch-worker-select")).toHaveValue("any");
  await dialog.getByRole("radio", { name: /^Codex CLI/ }).check();
  await expect(dialog.getByTestId("dispatch-outlook")).toContainText(`${live.worker.name}: no Codex CLI`);
  await dialog.getByRole("radio", { name: "Claude Code" }).check();
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

test("the dispatch form's Model field offers the models of the visitor's provider accounts, and the run asks for one", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  await liveWorker(me, project, uniqueName("laptop"));
  await putProviderByApi(me, "glm-runs", { provider: "glm", value: providerKey(), projects: [project], models: ["fake-large", "fake-coder"] });

  await open(page, `/p/${project}/runs`);
  await main(page).getByTestId("runs-empty-dispatch").click();
  const dialog = page.getByTestId("dispatch-dialog");
  await dialog.getByTestId("dispatch-step-2").click();
  const select = dialog.getByTestId("plan-run-model-select");
  // One group per provider, named by its prefix, with each model as a run asks for it.
  const group = select.locator('optgroup[data-testid="plan-run-model-provider"]');
  await expect(group).toHaveAttribute("label", "Provider glm");
  await expect(group.locator("option")).toHaveText(["glm/fake-coder", "glm/fake-large"]);
  await select.selectOption({ label: "glm/fake-large" });
  await expect(select).toHaveValue(/glm\/fake-large$/);
  // Left to any worker that fits: the one registered here reports no daemon version that runs a model through the gateway.
  await dialog.getByTestId("dispatch-worker-select").selectOption("any");
  await dialog.getByTestId("dispatch-submit").click();
  await expect(dialog).toBeHidden();
  const [run] = await runsOf(me, project);
  expect(run).toMatchObject({ step_key: "2", model: "glm/fake-large" });
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

  // The page opens on Running, which neither run is; All lists both.
  await open(page, `/p/${project}/runs`);
  const facets = main(page).getByTestId("runs-facets");
  await expect(facets.getByRole("button", { name: /^Running/ })).toHaveAttribute("aria-pressed", "true");
  await expect(main(page).getByTestId("state-empty")).toContainText("No run matches");
  await facets.getByRole("button", { name: /^All/ }).click();
  await expect(page).toHaveURL(/\?state=all$/);
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
  await none.getByRole("button", { name: "Clear filters" }).click(); // back to Running
  await expect(facets.getByRole("button", { name: /^Running/ })).toHaveAttribute("aria-pressed", "true");
  await facets.getByRole("button", { name: /^All/ }).click();
  await expect(main(page).getByTestId("runs-table").locator("tbody tr")).toHaveCount(2);

  // "/" focuses the search from anywhere on the page but a text field.
  await page.keyboard.press("/");
  await expect(main(page).getByTestId("runs-search")).toBeFocused();
  await expect(main(page).getByTestId("runs-search")).toHaveValue("");
  await main(page).getByTestId("runs-search").fill(STEP_TITLES["4"]);
  await expect(page).toHaveURL(new RegExp(`\\?state=all&q=${encodeURIComponent(STEP_TITLES["4"]).replace(/%20/g, "\\+")}$`));
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
  await expect(dialog.getByTestId("dispatch-worker-select")).toHaveValue(String(live.worker.id));
  await expect(dialog.getByTestId("dispatch-outlook")).toHaveCount(0);
  await dialog.getByRole("radio", { name: "Mark done" }).check();
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

test("a chat session is one row, as its latest turn, and opens into its turns, each under the words that woke it", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const handle = uniqueName("reviewer");
  await createAgentByApi(me, project, { handle, name: "Code reviewer" });
  const live = await chatWorker(me, project, uniqueName("mini"));
  const asks = ["please fix the flaky wait test", "one more thing: the retries", "and the docs?"];
  const {
    runs: [first, second, third],
  } = await chatSession(me, handle, live, asks);
  const session = `@${handle} in Release talk: @${handle} ${asks[0]}`;
  expect((await runsOf(me, project)).map((run) => [run.id, run.title, run.session_run_id, run.session_turn])).toEqual([
    [third, `@${handle} in Release talk: @${handle} ${asks[2]}`, first, 3],
    [second, `@${handle} in Release talk: @${handle} ${asks[1]}`, first, 2],
    [first, session, first, 1],
  ]);

  // One row: the session's title, whose session it is, how many turns, and the state of the latest.
  await open(page, `/p/${project}/runs`);
  const table = main(page).getByTestId("runs-table");
  await expect(table.locator("tbody tr")).toHaveCount(1);
  await expect(main(page).getByTestId("runs-list-summary")).toHaveText("1 run");
  const row = runRow(page, third);
  await expect(row.getByRole("link", { name: session, exact: true })).toHaveAttribute("href", `/p/${project}/runs/${third}`);
  await expect(row.getByTestId("run-where")).toHaveText(`Session of ${me.login} with @${handle} in a group chat`);
  await expect(row.getByTestId("run-state")).toHaveText("Running");

  // Opened, the row shows its turns in order, each titled by its own message, linked to its page.
  const toggle = row.getByTestId("runs-session-toggle");
  await expect(toggle).toHaveText("3 turns");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(toggle).toBeFocused();
  const turns = main(page).getByRole("list", { name: `Turns of the session ${session}` });
  const items = turns.getByTestId("session-turn");
  await expect(items).toHaveCount(3);
  expect(await items.evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-turn-run-id")))).toEqual([first, second, third].map(String));
  await expect(items.nth(1)).toContainText("Turn 2");
  await expect(items.nth(1).getByTestId("session-turn-link")).toHaveText(`@${handle} ${asks[1]}`);
  await expect(items.nth(1).getByTestId("run-state")).toHaveText("Done");
  await expect(items.nth(2).getByTestId("run-state")).toHaveText("Running");

  // The facets and the search read the session as its latest turn, and find it by any of its turns.
  const facets = main(page).getByTestId("runs-facets");
  await facets.getByRole("button", { name: /^Done/ }).click();
  await expect(main(page).getByTestId("state-empty")).toContainText("No run matches");
  await facets.getByRole("button", { name: /^Active/ }).click();
  await expect(table.locator("tbody tr").filter({ has: page.getByTestId("runs-session-toggle") })).toHaveCount(1);
  await facets.getByRole("button", { name: /^All/ }).click();
  await main(page).getByTestId("runs-search").fill("one more thing");
  await expect(page).toHaveURL(/\?state=all&q=one\+more\+thing$/);
  await expect(main(page).getByTestId("runs-list-summary")).toHaveText("1 run matches");
  await expect(runRow(page, third)).toBeVisible();

  // A turn of the list opens its own page. The empty list of Done closed the row, which opens again.
  await main(page).getByTestId("runs-search").fill("");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await toggle.click();
  await turns.getByTestId("session-turn-link").nth(1).click();
  await page.waitForURL(`**/p/${project}/runs/${second}`);
  await expect(main(page).getByTestId("run-turn")).toHaveText("Turn 2 of 3");
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
    await expect(dialog.getByRole("radio", { name: "Bất kỳ" })).toBeChecked();
  });
});
