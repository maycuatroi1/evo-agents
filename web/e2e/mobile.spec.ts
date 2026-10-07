import type { Locator, Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
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
 * "Filters (n)", every control is at least 44 px with 16 px text in fields, a run's page puts its decision and side
 * column before the trace, and a decision from the Inbox is a screen of its own with its answer in a bar at the foot.
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
      [`/p/${project}/runs`, "runs-table"],
      [`/p/${project}/plans`, "plans-table-active"],
    ]) {
      const html = await (await page.request.get(path, { headers: { "user-agent": agent } })).text();
      const sent = new RegExp(`data-testid="${testId}"[^>]*data-layout="${layout}"`).test(html);
      expect(sent, `the server's ${path} for a ${layout}`).toBe(true);
    }
  }

  await open(page, `/p/${project}/runs`);
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
  await expect(filters).toHaveText("Filters");
  await touchSized(filters, "Filters");
  expect(Math.abs((await box(filters)).y - (await box(search)).y), "Filters beside the search").toBeLessThan(2);

  await filters.click();
  const sheet = page.getByTestId("runs-filters");
  await expect(sheet.getByRole("heading", { name: "Filters" })).toBeVisible();
  const chips = sheet.getByTestId("runs-facets").getByRole("button");
  await expect(chips).toHaveCount(5);
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

  // Clear filters puts every run back.
  await filters.click();
  await sheet.getByTestId("filter-sheet-clear").click();
  await expect(sheet).toBeHidden();
  await expect(rows).toHaveCount(2);
  await expect(filters).toHaveText("Filters");

  // A tap anywhere on a row opens the run.
  await tapRow(main(page).locator(`li[data-run-id="${waiting.id}"]`));
  await expect(page).toHaveURL(new RegExp(`${runPath(project, waiting.id)}$`));
  await expect(main(page).getByTestId("run-detail")).toBeVisible();
});

test("a run's decision and side column come before its trace, and the raw log's groups are a menu", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await seedPlanRunPlan(me, project);
  const { live, run } = await planRunUnderway(me, project, uniqueName("phone"), { waiting: true });
  await sendEvents(live, run.id, [say("Reading the plan."), tool("Bash", "pnpm test")]);

  await open(page, runPath(project, run.id));
  const decision = main(page).getByTestId("run-decisions");
  await expect(decision.getByTestId("decision-form")).toBeVisible();
  const trace = main(page).getByTestId("run-log");
  await expect(main(page).getByTestId("trace-item").first()).toBeVisible();
  const order = [decision, main(page).getByTestId("run-side"), trace];
  const tops = [];
  for (const part of order) tops.push((await box(part)).y);
  expect(tops[0], "the decision before the side column").toBeLessThan(tops[1]);
  expect(tops[1], "the side column before the trace").toBeLessThan(tops[2]);
  const width = await page.evaluate(() => document.documentElement.scrollWidth);
  expect(width, "nothing scrolls sideways").toBeLessThanOrEqual(PHONE.width);

  // The raw log: its groups in one menu instead of a row of chips.
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
