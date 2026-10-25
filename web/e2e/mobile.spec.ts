import type { Locator, Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { createAgentByApi } from "./support/agents";
import { agentAnswers, chatPageOf, chatPath, chatWorker, claimChatRun, openDirectByApi, sendByApi } from "./support/chats";
import { expect, test } from "./support/fixtures";
import { ADMIN_ACCOUNT, machineToken, uniqueName } from "./support/hub";
import { apiOf, memoryFile, putMemory } from "./support/memories";
import { ACTIVE_PLAN, open, seedPlans } from "./support/plans";
import {
  askDecision,
  claimRun,
  decisionOf,
  dispatch,
  liveWorker,
  openRunDetails,
  planRunUnderway,
  reportState,
  runPath,
  say,
  seedPlanRunPlan,
  seedRunPlan,
  sendEvents,
  tool,
} from "./support/runs";
import { packSkill, publishSkill } from "./support/skills";
import { toast } from "./support/toast";
import { heartbeat, registerWorker, RUNTIMES } from "./support/workers";

/**
 * The hub on a phone (steps 14 and 15 of hub-ui-kit): a 375 x 812 screen with a phone's user agent and touch, so the
 * server renders the phone layout itself. Lists are rows that open the object's page, filters live in a sheet behind
 * "Filters (n)", every control is at least 44 px with 16 px text in fields, a run's page puts its head, its decision and
 * its trace's latest rows before its details and opens the full trace on a screen of its own, a decision from the
 * Inbox is a screen of its own with its answer in a bar at the foot, the Chats list's row of agents scrolls sideways
 * on its own, and a direct chat's bubbles fit the screen with React and Reply under each.
 */
const PHONE = { width: 375, height: 812 };
const IPHONE_UA =
  "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1";
const DESKTOP_UA =
  "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36";

test.use({ viewport: PHONE, userAgent: IPHONE_UA, hasTouch: true, isMobile: true });

const main = (page: Page) => page.locator("#main");

async function box(locator: Locator) {
  const found = await locator.boundingBox();
  expect(found, "the element is laid out").not.toBeNull();
  return found!;
}

/** Each control at least 44 px tall and wide, measured once menus and sheets have settled (they zoom in from 95%). */
async function touchSized(locator: Locator, label: string) {
  await locator.page().waitForFunction(() => document.getAnimations().every((animation) => animation.playState !== "running"));
  const { width, height } = await box(locator);
  expect.soft(height, `${label} is 44 px tall`).toBeGreaterThanOrEqual(44);
  expect.soft(width, `${label} is 44 px wide`).toBeGreaterThanOrEqual(44);
}

async function fieldText(locator: Locator): Promise<string> {
  return locator.evaluate((element) => getComputedStyle(element).fontSize);
}

/** The list of a list page, once it is the phone's list. */
function list(page: Page, testId: string) {
  return main(page).locator(`[data-testid="${testId}"][data-layout="list"]`);
}

/** Tap a row away from its title, on its meta line: the row's link covers it, so the tap opens the row's page. */
async function tapRow(row: Locator) {
  const { height } = await box(row);
  await row.click({ position: { x: 24, y: height - 14 } });
}

test("the runs list starts on the first screen, and its filters apply from a sheet", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("phone"));
  const [failing, waiting] = await dispatch(me, project, ["2", "4"]);
  await claimRun(live);
  await reportState(live, failing.id, { state: "failed", error: "verify failed: pnpm test exited 1" });

  // The server already sends a phone the list (and a desktop the table), so nothing swaps once the scripts run.
  await seedPlans(me, project);
  for (const [agent, layout] of [
    [IPHONE_UA, "list"],
    [DESKTOP_UA, "table"],
  ]) {
    for (const [path, testId] of [
      [`/p/${project}/runs?state=all`, "runs-table"],
      [`/p/${project}/plans`, "plans-table-active"],
    ]) {
      const html = await (await page.request.get(path, { headers: { "user-agent": agent } })).text();
      const sent = new RegExp(`data-testid="${testId}"[^>]*data-layout="${layout}"`).test(html);
      expect(sent, `the server's ${path} for a ${layout}`).toBe(true);
    }
  }

  // Every run: the page opens on Running, and these two are failed and queued.
  await open(page, `/p/${project}/runs?state=all`);
  const rows = list(page, "runs-table").getByRole("listitem");
  await expect(rows).toHaveCount(2);
  const first = await box(rows.first());
  expect(first.y, "the first run's top edge is on the first screen").toBeLessThan(PHONE.height);
  expect(first.y + first.height, "and the whole row too").toBeLessThanOrEqual(PHONE.height);
  expect(first.height).toBeGreaterThanOrEqual(56);
  // A row says the run's state and one line; the rest is on the run's page, which the row opens.
  const failed = main(page).locator(`li[data-run-id="${failing.id}"]`);
  await expect(failed.getByTestId("run-state")).toHaveText(/Failed/);
  await expect(failed).toContainText("verify failed: pnpm test exited 1");

  // The toolbar is one row: the 44 px search with 16 px text, and Filters.
  const search = main(page).getByTestId("runs-search");
  await touchSized(search.locator("xpath=ancestor::form[1]"), "the search field");
  expect(await fieldText(search)).toBe("16px");
  await expect(main(page).getByTestId("runs-facets")).toHaveCount(0);
  const filters = main(page).getByTestId("runs-filters-open");
  // All is a filter of its own, the page's default being Running.
  await expect(filters).toHaveText("Filters (1)");
  await touchSized(filters, "Filters");
  expect(Math.abs((await box(filters)).y - (await box(search)).y), "Filters beside the search").toBeLessThan(2);

  await filters.click();
  const sheet = page.getByTestId("runs-filters");
  await expect(sheet.getByRole("heading", { name: "Filters" })).toBeVisible();
  const chips = sheet.getByTestId("runs-facets").getByRole("button");
  await expect(chips).toHaveCount(7);
  for (const chip of await chips.all()) await touchSized(chip, `the chip ${await chip.textContent()}`);
  await expectNoSeriousViolations(page, "runs filter sheet at 375 px");

  await sheet.locator('[data-facet-value="ended"]').click();
  await expect(page).toHaveURL(/[?&]state=ended\b/);
  await expect(sheet.getByTestId("filter-sheet-summary")).toHaveText("1 run matches");
  await sheet.getByTestId("filter-sheet-done").click();
  await expect(sheet).toBeHidden();
  await expect(filters).toHaveText("Filters (1)");
  await expect(filters).toBeFocused();
  await expect(rows).toHaveCount(1);
  await expect(main(page).locator(`li[data-run-id="${failing.id}"]`)).toBeVisible();

  // Clear filters goes back to the page's default, Running, where neither run is; All puts both back.
  await filters.click();
  await sheet.getByTestId("filter-sheet-clear").click();
  await expect(sheet).toBeHidden();
  await expect(filters).toHaveText("Filters");
  await expect(rows).toHaveCount(0);
  await filters.click();
  await sheet.locator('[data-facet-value=""]').first().click();
  await sheet.getByTestId("filter-sheet-done").click();
  await expect(sheet).toBeHidden();
  await expect(rows).toHaveCount(2);

  // A tap anywhere on a row opens the run.
  await tapRow(main(page).locator(`li[data-run-id="${waiting.id}"]`));
  await expect(page).toHaveURL(new RegExp(`${runPath(project, waiting.id)}$`));
  await expect(main(page).getByTestId("run-detail")).toBeVisible();
});

test("a run's head, decision and latest trace rows come before its details, its full trace is a screen of its own, and the raw log's groups are a menu", async ({
  page,
  member,
}) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { live, run } = await planRunUnderway(me, project, uniqueName("phone"), { waiting: true });
  const inWorktree = (command: string) => `cd /home/me/.evo/worker/worktrees/demo && export PATH=/tmp/venv/bin:$PATH && ${command}`;
  await sendEvents(live, run.id, [
    say("Reading the plan."),
    tool("Bash", "pnpm test"),
    { kind: "output", body: { type: "system", subtype: "task_progress" } },
    tool("Bash", inWorktree("ruff check .")),
    tool("Bash", inWorktree("pytest -q tests/hub")),
  ]);

  // The overview: the head (the plan, its step and progress), the decision, then the trace's latest rows, newest first,
  // each a group of calls named by what it did with its command without the setup; the session is not on this page.
  await open(page, runPath(project, run.id));
  const head = main(page).getByTestId("run-header");
  await expect(head.getByTestId("run-current-step")).toHaveText("Step 2: Run plan dialog");
  await expect(head.getByTestId("run-progress-text")).toHaveText("1/4 steps");
  await expect(head.getByTestId("run-status-line")).toContainText("Waiting for an answer for");
  const decision = main(page).getByTestId("run-decisions");
  await expect(decision.getByTestId("decision-form")).toBeVisible();
  const preview = main(page).getByTestId("run-trace-preview");
  const rows = preview.getByTestId("run-trace-preview-row");
  await expect(rows).toHaveCount(4);
  expect(await rows.evaluateAll((items) => items.map((item) => item.getAttribute("data-type")))).toEqual(["group", "group", "group", "agent"]);
  await expect(rows.nth(0)).toContainText("Ran tests");
  await expect(rows.nth(0)).toContainText("pytest -q tests/hub");
  await expect(rows.nth(1)).toContainText("Ran a check");
  await expect(rows.nth(1)).toContainText("ruff");
  await expect(rows.nth(2)).toContainText("Ran tests");
  await expect(rows.nth(2)).toContainText("pnpm test");
  await expect(rows.nth(3)).toContainText("Reading the plan.");
  await expect(preview).not.toContainText("cd /home");
  await expect(preview).not.toContainText("export PATH");
  await expect(preview).toContainText("3 tool calls");
  await expect(main(page).getByTestId("run-log")).toHaveCount(0);
  await expect(main(page).getByTestId("trace-raw-left")).toHaveCount(0);
  await expect(main(page).getByTestId("run-side")).toHaveCount(0);
  const order = [head, decision, preview];
  const tops = [];
  for (const part of order) tops.push((await box(part)).y);
  expect(tops, "the head, the decision, then the trace's preview").toEqual([...tops].sort((a, b) => a - b));
  let width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width, "nothing scrolls sideways").toBeLessThanOrEqual(PHONE.width);
  await touchSized(head.getByTestId("run-details-open"), "the Details toggle");
  await touchSized(head.getByTestId("run-actions-menu"), "the menu of the run's actions");
  await touchSized(preview.getByTestId("run-open-trace"), "Open the full trace");
  await expectNoSeriousViolations(page, "run page at 375 px");

  // Details opens the side cards after the preview.
  await openRunDetails(page);
  const side = main(page).getByTestId("run-side");
  await expect(side.getByTestId("run-plan-steps")).toBeVisible();
  expect((await box(side)).y, "the side cards after the trace's preview").toBeGreaterThan((await box(preview)).y);

  // Open the full trace: a screen of its own with a way back, the calls as one row per kind of work, each named once,
  // its command without its setup, and the runtime event the trace does not read left to the Raw log.
  await preview.getByTestId("run-open-trace").click();
  const screen = main(page).getByTestId("run-detail");
  await expect(screen).toHaveAttribute("data-screen", "session");
  await expect(screen.getByRole("heading", { level: 1 })).toHaveText(`Run #${run.id}`);
  await expect(main(page).getByTestId("run-trace-preview")).toHaveCount(0);
  const trace = main(page).getByTestId("run-log");
  await expect(trace.getByTestId("run-tab-trace")).toHaveAttribute("aria-selected", "true");
  const groups = trace.getByTestId("trace-group");
  await expect(groups).toHaveCount(3);
  expect(await groups.evaluateAll((items) => items.map((item) => item.getAttribute("data-intent")))).toEqual(["test", "check", "test"]);
  await expect(trace.getByTestId("trace-group-title")).toHaveText(["Ran tests", "Ran a check", "Ran tests"]);
  await expect(trace.getByTestId("trace-group-detail")).toHaveText(["pnpm test", "ruff", "pytest -q tests/hub"]);
  await expect(groups.last()).toHaveAttribute("data-running", "true");
  await expect(trace.getByTestId("trace-typing")).toHaveCount(0); // the running group's clock is what moves
  await expect(trace.getByTestId("trace-raw-left")).toContainText("1 runtime event");
  await touchSized(groups.nth(1).getByTestId("trace-group-toggle"), "a row of the trace");
  await groups.nth(1).getByTestId("trace-group-toggle").click();
  await expect(groups.nth(1).getByTestId("trace-tool-arg")).toHaveText("ruff check .");
  await touchSized(groups.nth(1).locator("summary"), "a call of a row of the trace");
  width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width, "nothing scrolls sideways on the trace's screen").toBeLessThanOrEqual(PHONE.width);
  await touchSized(main(page).getByTestId("run-screen-back"), "the way back to the run");
  await expectNoSeriousViolations(page, "the trace's screen at 375 px");

  // The raw log, a tab of the screen: its groups in one menu instead of a row of chips.
  await trace.getByTestId("run-tab-log").click();
  await expect(trace.getByTestId("log-line").first()).toBeVisible();
  await expect(trace.getByTestId("log-facets")).toHaveCount(0);
  const menu = trace.getByTestId("log-group-menu");
  await expect(menu).toHaveAccessibleName("Show: All");
  await touchSized(menu, "the log's group menu");
  expect(await fieldText(trace.getByTestId("log-search"))).toBe("16px");
  await menu.click();
  const tools = page.getByTestId("log-group-tools");
  await touchSized(tools, "a group in the menu");
  await expectNoSeriousViolations(page, "raw log group menu at 375 px");
  await tools.click();
  await expect(menu).toHaveAccessibleName("Show: Tools");
  await expect(menu).toHaveAttribute("data-group", "tools");
  const kinds = await trace.getByTestId("log-line").evaluateAll((lines) => lines.map((line) => line.getAttribute("data-kind")));
  expect(kinds.length).toBeGreaterThan(0);
  expect(new Set(kinds)).toEqual(new Set(["tool_call"]));

  // Back, the button or the browser's, leads to the overview; the session's tab is the Trace again.
  await main(page).getByTestId("run-screen-back").click();
  await expect(main(page).getByTestId("run-detail")).not.toHaveAttribute("data-screen", "session");
  await expect(main(page).getByTestId("run-trace-preview")).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`${runPath(project, run.id)}$`));
  await main(page).getByTestId("run-open-trace").click();
  await expect(main(page).getByTestId("run-detail")).toHaveAttribute("data-screen", "session");
  await page.goBack();
  await expect(main(page).getByTestId("run-trace-preview")).toBeVisible();
  await expect(main(page).getByTestId("run-log")).toHaveCount(0);
});

test("every list is a list of rows that open their page", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "customer" }]);
  const project = me.projects[0];
  await seedPlans(me, project);
  const api = await apiOf(me);
  const memory = await putMemory(api, { project, name: "runbook.md", body: memoryFile("Runbook", "How we ship", "Tag, then ship.") });
  await publishSkill(api, await packSkill("team-notes", "Notes.", "Use when writing notes"), "team-notes", project);
  const worker = await registerWorker(me, { name: uniqueName("phone"), projects: [project] });
  await heartbeat(worker.id, { runtimes: RUNTIMES });

  const pages: { path: string; testId: string; row: string; lands: RegExp }[] = [
    { path: `/p/${project}/plans`, testId: "plans-table-active", row: `[data-plan-id="${ACTIVE_PLAN}"]`, lands: new RegExp(`/plans/${ACTIVE_PLAN}$`) },
    { path: `/p/${project}/memories`, testId: "memories-table", row: '[data-memory-name="runbook.md"]', lands: new RegExp(`/memories/${memory.id}$`) },
    { path: `/p/${project}/skills`, testId: "skills-table", row: '[data-skill-name="team-notes"]', lands: /\/skills\/team-notes$/ },
    { path: "/workers", testId: "workers-table", row: `[data-worker-name="${worker.name}"]`, lands: new RegExp(`/workers/${worker.id}$`) },
  ];
  for (const visit of pages) {
    await open(page, visit.path);
    const row = list(page, visit.testId).locator(visit.row);
    await expect(row, visit.path).toBeVisible();
    expect((await box(row)).height, `${visit.path}: a row is a touch target`).toBeGreaterThanOrEqual(56);
    await expect(main(page).getByRole("table")).toHaveCount(0);
    await tapRow(row);
    await expect(page, visit.path).toHaveURL(visit.lands);
  }

  // The plan's steps as a list too, with a 44 px view switch.
  await open(page, `/p/${project}/plans/${ACTIVE_PLAN}`);
  await touchSized(main(page).getByTestId("steps-view-list"), "the List view button");
  await main(page).getByTestId("steps-view-list").click();
  await expect(list(page, "steps-table").getByRole("listitem").first()).toBeVisible();

  // The memories' two facets in one sheet, each group labelled.
  await open(page, `/p/${project}/memories`);
  await main(page).getByTestId("memories-filters-open").click();
  const sheet = page.getByTestId("memories-filters");
  await expect(sheet.getByRole("group", { name: "Type" })).toBeVisible();
  await expectNoSeriousViolations(page, "memories filter sheet at 375 px");
});

test("the plan's graph opens a step in a bottom sheet, its controls 44 px, and gives the focus back on close", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "customer" }]);
  const project = me.projects[0];
  await seedPlans(me, project);
  await open(page, `/p/${project}/plans/${ACTIVE_PLAN}?view=graph`);
  const canvas = main(page).getByTestId("step-graph-canvas");
  await expect(canvas).toHaveAttribute("data-ready", "true");
  // The zoom buttons sit in the bar above the canvas, so no step is ever under them.
  const bar = main(page).getByTestId("graph-legend");
  for (const name of ["Zoom in", "Zoom out", "Fit the plan"]) await touchSized(bar.getByRole("button", { name, exact: true }), name);

  const node = canvas.locator('[data-testid="graph-step"][data-step="10"]');
  await node.click();
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByTestId("inspector-body")).toHaveAttribute("data-step", "10");
  await expect(page).toHaveURL(/[?&]step=10(&|$)/);
  await expect(sheet.getByTestId("inspector-title")).toBeFocused();
  for (const id of ["inspector-previous", "inspector-next", "inspector-close", "inspector-open-page"]) await touchSized(sheet.getByTestId(id), id);
  for (const button of await sheet.getByRole("button", { name: /^Select step/ }).all()) {
    await touchSized(button, `the step button ${await button.getAttribute("data-step")}`);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(PHONE.width);
  await expectNoSeriousViolations(page, "step inspector sheet at 375 px");

  await sheet.getByTestId("inspector-close").click();
  await expect(sheet).toHaveCount(0);
  await expect(page).not.toHaveURL(/[?&]step=/);
  await expect(page.locator(".react-flow__node:focus").getByTestId("graph-step")).toHaveAttribute("data-step", "10");
});

test("the admin lists are rows, and their filter forms apply from the sheet", async ({ page, signInAs }) => {
  await signInAs(ADMIN_ACCOUNT);
  await machineToken(ADMIN_ACCOUNT); // a machine token next to the web sessions

  await open(page, "/admin/members");
  await expect(list(page, "members-table").locator(`[data-login="${ADMIN_ACCOUNT.login}"]`)).toBeVisible();
  const projectFilter = main(page).getByTestId("member-filters-open");
  await touchSized(projectFilter, "Filters");

  await open(page, "/admin/audit");
  await expect(list(page, "audit-table").getByRole("listitem").first()).toBeVisible();

  await open(page, "/admin/tokens");
  const tokens = list(page, "tokens-table").getByRole("listitem");
  await expect(tokens.first()).toBeVisible();
  const revoke = tokens.locator('[data-testid^="revoke-token-"]').first();
  await touchSized(revoke, "Revoke");
  await expect(revoke).toHaveAccessibleName(/^Revoke token \d+ of /);

  const filters = main(page).getByTestId("token-filters-sheet-open");
  await filters.click();
  const form = page.getByTestId("token-filters");
  await expect(form.getByRole("combobox", { name: "Kind" })).toBeVisible();
  for (const control of await form.locator("input, select").all()) {
    await touchSized(control, `the field ${await control.getAttribute("name")}`);
    expect(await fieldText(control)).toBe("16px");
  }
  await expectNoSeriousViolations(page, "token filters at 375 px");
  await form.getByRole("combobox", { name: "Kind" }).selectOption("machine");
  await form.getByTestId("token-filters-apply").click();
  await expect(form).toBeHidden();
  await expect(page).toHaveURL(/[?&]kind=machine\b/);
  await expect(filters).toHaveText("Filters (1)");
  await expect(tokens.first()).toContainText("Machine token");
});

test("a decision is answered on a screen of its own, its bar in reach while the screen scrolls", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  // The agent asks while it works, on a worker that allows the web terminal: Take over is offered beside Send answer.
  const { live, run } = await planRunUnderway(me, project, uniqueName("phone"), { terminal: true });
  await sendEvents(live, run.id, [say("Reading the plan."), tool("Bash", "pnpm test")]);
  const question = "Publish evo-agents 0.4.0 to PyPI now?";
  const decision = await askDecision(live, run.id, question, "3", {
    options: [
      { key: "publish", label: "Publish now", description: "Upload 0.4.0, tag v0.4.0, then go on with step 14." },
      { key: "hold", label: "Hold until step 14 is done", description: "Keep the build, skip the upload." },
      { key: "stop", label: "Stop the plan run", description: "You publish by hand." },
    ],
    recommended: "publish",
  });

  await open(page, `/inbox?decision=${decision}`);
  const screen = page.getByTestId("decision-sheet");
  await expect(screen).toHaveAttribute("data-layout", "screen");
  await expect(screen).toHaveAccessibleName(question);
  const frame = await box(screen);
  expect([frame.x, frame.width, frame.height], "the screen is the whole phone").toEqual([0, PHONE.width, PHONE.height]);

  // The 52 px top bar: Back to Inbox, the run, whether the page is current, and the bell.
  const top = screen.getByTestId("decision-screen-top");
  expect((await box(top)).height).toBe(52);
  await touchSized(top.getByRole("button", { name: "Back to Inbox" }), "Back to Inbox");
  await expect(top.getByTestId("decision-screen-run")).toHaveText(`Run #${run.id}`);
  await expect(top.getByTestId("live-indicator")).toBeVisible();
  await touchSized(top.getByTestId("inbox-bell"), "the bell");

  // The question is the screen's h1, the options have 14 px of padding with the agent's pick chosen, the note 16 px text.
  await expect(screen.getByRole("heading", { level: 1 })).toHaveText(question);
  const choice = screen.getByTestId("decision-choice-publish");
  expect(await choice.evaluate((element) => getComputedStyle(element).padding)).toBe("14px");
  await expect(screen.getByRole("radio", { checked: true })).toHaveAccessibleName("Publish now Agent's pick");
  const note = screen.getByTestId("decision-text");
  expect(await fieldText(note)).toBe("16px");

  // The bar at the foot, on the screen's bottom edge: Take over and Send answer side by side, 44 px, no key on a phone.
  const bar = screen.getByTestId("decision-bar");
  const send = bar.getByTestId("decision-send");
  const takeOver = bar.getByTestId("decision-takeover");
  await touchSized(send, "Send answer");
  await touchSized(takeOver, "Take over");
  expect((await box(send)).height).toBe(44);
  expect(Math.abs((await box(send)).y - (await box(takeOver)).y), "Take over beside Send answer").toBeLessThan(1);
  await expect(bar.getByTestId("decision-send-kbd")).toHaveCount(0);
  const docked = async () => {
    const edge = await box(bar);
    expect(Math.round(edge.y + edge.height), "the bar sits on the bottom edge").toBe(PHONE.height);
  };
  await docked();

  // What the agent did so far: folded, and once opened its last things, oldest first.
  const soFar = screen.getByTestId("decision-so-far");
  await expect(soFar).not.toHaveAttribute("open", "");
  await soFar.getByText("What the agent did so far").click();
  const items = soFar.getByTestId("decision-so-far-item");
  await expect(items.last()).toContainText("Bash pnpm test");
  await expect(items.nth(-2)).toContainText("Agent Reading the plan.");
  expect(await items.count()).toBeLessThanOrEqual(5);
  await expectNoSeriousViolations(page, "decision screen at 375 px");

  // Scrolled to the end and back to the top, the bar stays on the bottom edge.
  const panel = screen.getByTestId("decision-panel");
  await note.fill(Array.from({ length: 8 }, (_, index) => `Line ${index + 1} of the note.`).join("\n"));
  expect(await panel.evaluate((element) => element.scrollHeight - element.clientHeight), "the screen's middle scrolls").toBeGreaterThan(0);
  await panel.evaluate((element) => element.scrollTo({ top: element.scrollHeight }));
  await expect.poll(() => panel.evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
  await expect(bar).toBeInViewport({ ratio: 1 });
  await docked();
  await expect(soFar).toBeInViewport();
  await panel.evaluate((element) => element.scrollTo({ top: 0 }));
  await expect(bar).toBeInViewport({ ratio: 1 });
  await docked();

  // Send answer answers with the agent's pick and the note; the bar goes with the form.
  await note.fill("After the backup.");
  await send.click();
  await expect(toast(page, `Answer sent to run #${run.id}`)).toBeVisible();
  await expect(screen.getByTestId("decision-answer")).toContainText("Publish now");
  await expect(bar).toHaveCount(0);
  const answered = await decisionOf(me, project, decision);
  expect([answered.state, answered.answer_option, answered.answer_text]).toEqual(["answered", "publish", "After the backup."]);

  // Back to Inbox: the list, with focus on the decision's link.
  await screen.getByTestId("decision-back").click();
  await expect(page).toHaveURL(/\/inbox$/);
  await expect(page.getByTestId("decision-sheet")).toHaveCount(0);
  await expect(main(page).locator(`[data-decision-link="${decision}"]`)).toBeFocused();
});

test("a decision's screen says why the agent picks its option, how to undo the act and what it rests on, each ref a link", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { live, run } = await planRunUnderway(me, project, uniqueName("phone-why"));
  const decision = await askDecision(live, run.id, "Drop the staging bucket of the old build now?", "3", {
    category: "delete_data",
    options: [
      { key: "drop", label: "Drop it now" },
      { key: "keep", label: "Keep it a week" },
    ],
    recommended: "drop",
    recommended_reason: "Nothing reads it since the cut-over, and it costs 40 USD a month.",
    reversible: true,
    undo: "Restore it from last night's snapshot within 7 days.",
    evidence: [`run:${run.id}`, "https://console.example.test/buckets/staging-old", "code:api:deploy/buckets.tf:12"],
  });

  await open(page, `/inbox?decision=${decision}`);
  const screen = page.getByTestId("decision-sheet");
  await expect(screen).toHaveAttribute("data-layout", "screen");
  const grounds = screen.getByRole("region", { name: "Why, how to undo it, and the evidence" });
  await expect(grounds.getByTestId("decision-reason")).toHaveText("Why “Drop it now”: Nothing reads it since the cut-over, and it costs 40 USD a month.");
  await expect(grounds.getByTestId("decision-undo")).toHaveText("Can be undone: Restore it from last night's snapshot within 7 days.");
  expect(await grounds.getByTestId("decision-reason").locator("p").evaluate((element) => getComputedStyle(element).fontSize)).toBe("15px");
  const links = grounds.getByTestId("decision-evidence-link");
  await expect(links).toHaveCount(3);
  await expect(links.nth(0)).toHaveAttribute("href", `/p/${project}/runs/${run.id}`);
  await expect(links.nth(1)).toHaveAttribute("href", "https://console.example.test/buckets/staging-old");
  await expect(links.nth(1)).toHaveAttribute("target", "_blank");
  await expect(links.nth(2)).toHaveAttribute("href", `/p/${project}/kg?q=deploy%2Fbuckets.tf`);
  for (const item of await grounds.getByTestId("decision-evidence-item").all()) {
    expect((await box(item)).height, "each ref is a touch target").toBeGreaterThanOrEqual(44);
  }
  // The grounds sit between the question and the answer's options.
  expect((await box(grounds)).y).toBeGreaterThan((await box(screen.getByRole("heading", { level: 1 }))).y);
  expect((await box(grounds)).y).toBeLessThan((await box(screen.getByTestId("decision-choice-drop"))).y);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  await expectNoSeriousViolations(page, "decision screen with its grounds at 375 px");

  await links.nth(0).click();
  await expect(page).toHaveURL(new RegExp(`/p/${project}/runs/${run.id}$`));
});

test("the chats list's row of agents scrolls sideways on its own while the page does not, and an agent opens its chat as a page", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  for (const name of ["Code reviewer", "Docs writer", "Release captain", "Flaky test hunter", "Triage desk", "Night shift"]) {
    await createAgentByApi(me, project, { handle: uniqueName(name.split(" ")[0].toLowerCase()), name });
  }

  await open(page, "/chats");
  const row = main(page).getByTestId("agent-strip-agents");
  const agents = row.getByTestId("agent-strip-agent");
  await expect(agents).toHaveCount(7);
  const widths = await row.evaluate((element) => ({ scroll: element.scrollWidth, client: element.clientWidth }));
  expect(widths.scroll, "the row holds more than the screen").toBeGreaterThan(widths.client);
  await row.evaluate((element) => element.scrollBy({ left: 240 }));
  await expect.poll(() => row.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
  const layout = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: window.innerWidth, x: window.scrollX }));
  expect(layout.scroll, "the page never scrolls sideways").toBeLessThanOrEqual(layout.width);
  expect(layout.x).toBe(0);
  await touchSized(agents.first(), "an agent of the row");

  // The last agent, out of view at first: tapped, its chat is the page, its composer focused, a way back before its face.
  await agents.last().tap();
  await page.waitForURL(/\/chats\/[0-9]+$/);
  await expect(main(page).getByTestId("chats-list")).toBeHidden();
  await expect(main(page).getByTestId("chat-head")).toHaveAttribute("data-kind", "direct");
  await expect(main(page).getByTestId("chat-composer-text")).toBeFocused();
  await expect(main(page).getByTestId("chat-back")).toBeVisible();
  // The way back, the face, the name and Details (its icon, a touch target) share one row; the handle, the project
  // and the visibility sit under the name, so the head stays a small part of the screen.
  const back = await box(main(page).getByTestId("chat-back"));
  const title = await box(main(page).getByTestId("chat-title"));
  const details = main(page).getByTestId("chat-details-open");
  const detailsBox = await box(details);
  expect(Math.abs(back.y + back.height / 2 - (title.y + title.height / 2)), "the way back sits on the name's row").toBeLessThan(8);
  expect(back.x + back.width, "the way back stands before the name").toBeLessThanOrEqual(title.x);
  expect(Math.abs(detailsBox.y + detailsBox.height / 2 - (title.y + title.height / 2)), "Details sits on the name's row").toBeLessThan(8);
  expect(detailsBox.x, "Details ends the name's row").toBeGreaterThanOrEqual(title.x + title.width);
  await expect(details).toHaveAccessibleName("Details");
  await touchSized(details, "Details in a direct chat's head");
  const visibility = await box(main(page).getByTestId("chat-visibility").first());
  expect(visibility.y, "the visibility sits under the name").toBeGreaterThan(title.y + title.height);
  const head = await box(main(page).getByTestId("chat-head"));
  expect(head.height, "the head takes less than a sixth of the screen").toBeLessThan(PHONE.height / 6);
  await expectNoSeriousViolations(page, "a direct chat at 375 px");
});

test("a direct chat at 375 px: the member's bubbles on the right, the agent's under them on the left, nothing sideways, Reply a touch target", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const live = await chatWorker(me, project, uniqueName("phone-chat"));
  const chat = await openDirectByApi(me, project);
  const asked = await sendByApi(me, chat.id, "Why did https://github.com/example-org/api/actions/runs/123456789012345/job/987654321098765 fail?");
  const runId = asked.mentions[0]?.run_id;
  if (!runId) throw new Error(`the message made no run: ${JSON.stringify(asked.mentions)}`);
  await claimChatRun(live, runId);
  await agentAnswers(live, runId, `The clock ran out:\n\n\`\`\`ts\nawait clock.tickAsync(500); // ${"waits on the clock ".repeat(8)}\n\`\`\``);

  await open(page, chatPath(chat.id));
  const log = main(page).getByTestId("chat-log");
  const mine = log.locator('[data-testid="chat-message"][data-mine="true"]');
  const theirs = log.locator('[data-testid="chat-message"][data-author="agent"]');
  await expect(theirs).toHaveCount(1);
  const area = await box(log);
  const ours = await box(mine.getByTestId("message-bubble"));
  const answer = await box(theirs.getByTestId("message-bubble"));
  expect(area.x + area.width - (ours.x + ours.width), "the member's bubble ends at the right").toBeLessThan(24);
  expect(ours.width, "the member's bubble leaves the left of the screen free").toBeLessThan(area.width * 0.9);
  expect(answer.x, "the agent's bubble starts at the left, after its face").toBeLessThan(ours.x);
  expect(answer.y, "the answer sits under the message").toBeGreaterThan(ours.y);
  expect(answer.x + answer.width, "the agent's bubble fits the screen").toBeLessThanOrEqual(area.x + area.width);
  const widths = await log.evaluate((element) => ({ scroll: element.scrollWidth, client: element.clientWidth }));
  expect(widths.scroll, "the messages never scroll sideways").toBeLessThanOrEqual(widths.client);
  const layout = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: window.innerWidth }));
  expect(layout.scroll, "the page never scrolls sideways").toBeLessThanOrEqual(layout.width);

  // React and Reply sit under each message on a phone, each a touch target; Reply puts the answer above the box.
  const reply = theirs.getByTestId("message-reply");
  await expect(reply).toBeVisible();
  await touchSized(reply, "Reply under the agent's answer");
  await touchSized(theirs.getByTestId("reaction-add"), "React under the agent's answer");
  await reply.tap();
  await expect(main(page).getByTestId("chat-composer-replying")).toContainText("The clock ran out:");
  await touchSized(main(page).getByTestId("chat-composer-reply-cancel"), "Cancel the reply");
  const composer = main(page).getByTestId("chat-composer-text");
  expect(await fieldText(composer)).toBe("16px");
  await expectNoSeriousViolations(page, "a direct chat with answers at 375 px");

  // A phone's keyboard has no Shift with Enter: Enter adds a line there, and Send sends.
  await expect(composer).toHaveAttribute("aria-keyshortcuts", "Meta+Enter Control+Enter");
  await composer.fill("Then raise the timeout.");
  await composer.press("Enter");
  await composer.pressSequentially("To 2 s.");
  await expect(composer).toHaveValue("Then raise the timeout.\nTo 2 s.");
  await expect(composer).toBeFocused();
  await main(page).getByTestId("chat-composer-send").tap();
  await expect(composer).toHaveValue("");
  await expect.poll(async () => (await chatPageOf(me, chat.id, asked.id)).messages.map((item) => item.body)).toContain("Then raise the timeout.\nTo 2 s.");
});
