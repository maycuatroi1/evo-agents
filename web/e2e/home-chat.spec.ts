import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { createAgentByApi } from "./support/agents";
import { agentSays, chatPageOf, chatWorker, claimChatRun, openDirectByApi, sendByApi, takeInbox } from "./support/chats";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { sendOutputs } from "./support/outputs";
import { open } from "./support/plans";
import { finishWork, listedWork, seedWork, setWork, workSystem } from "./support/work";

/**
 * The chat home (plan chat-first-home, criterion b3; docs/hub.md, Chat home): Home `/` is a chat with one of the agents
 * the member may give work to (GET /v1/me/chat-agents), the one written to last chosen first. Before a first message
 * the agent's composer sits in the middle of the frame with its starters; the first message opens the member's direct
 * chat with the agent, and the chat's own components show it, interactive answers included. With no agent the page
 * says why and leads to the projects, on Today. In light and dark, at 1440 and 375 px, axe finds nothing serious and
 * the page never scrolls sideways. Home has no top bar and no sidebar (plan agent-workspace-home, a5 to a7): a rail
 * lists the agent's tasks in Needs you, Running, This week and Earlier, and a task opens in the main pane, named by
 * `?task=`. The specs play the agent's worker; no model is asked anything.
 */
test.skip(isDeployed, "runs a fake worker against the local stack");

const STARTERS = ["Compare the two plans", "What failed last night?"];

const ANSWER_PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Plans compared</title></head>
<body><main><h1>Plans compared</h1><p id="out">The second plan ships two weeks sooner.</p></main></body></html>`;

/** An agent of `project` the member may chat with: made by them (an admin), with a worker of theirs in its pool. */
async function agentOf(me: Member, project: string, name: string, starters: string[] = []) {
  const handle = uniqueName(name.toLowerCase());
  await createAgentByApi(me, project, { handle, name, starters });
  const live = await chatWorker(me, project, uniqueName("chat-box"), 1, handle);
  return { handle, live };
}

const pane = (page: Page) => page.getByTestId("home-main");
const rail = (page: Page) => page.getByTestId("home-rail");
const taskRow = (page: Page, chatId: number) => rail(page).locator(`[data-testid="home-task"][data-chat-id="${chatId}"]`);
const picker = (page: Page) => page.getByTestId("chat-home-picker");

async function noSidewaysScroll(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
}

test("Home chooses the agent written to last, and starts a chat with another from its starters", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const analyst = await agentOf(me, project, "Analyst", STARTERS);
  const scribe = await agentOf(me, project, "Scribe");
  // The member wrote to Scribe: it comes first, and Home opens on its direct chat.
  const direct = await openDirectByApi(me, scribe.handle);
  await sendByApi(me, direct.id, "Draft the release notes");

  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, "/");
  await expect(picker(page)).toHaveAttribute("data-agent", scribe.handle);
  await expect(page.getByTestId("agent-chat")).toHaveAttribute("data-frame", "chat");
  await expect(page.getByTestId("chat-view")).toHaveAttribute("data-chat-id", String(direct.id));
  await expect(page.getByTestId("chat-message").filter({ hasText: "Draft the release notes" })).toBeVisible();

  // The picker lists the project's agents (its default agent too), the one written to last first; choosing Analyst
  // shows its start, named in the address.
  await picker(page).click();
  const menu = page.getByTestId("chat-home-picker-menu");
  await expect(menu.getByTestId("chat-home-agent")).toHaveCount(3);
  await expect(menu.getByTestId("chat-home-agent").first()).toHaveAttribute("data-agent", scribe.handle);
  await expect(menu.locator(`[data-testid="chat-home-agent"][data-agent="${project}"]`)).toHaveCount(1);
  await menu.locator(`[data-testid="chat-home-agent"][data-agent="${analyst.handle}"]`).click();
  await expect(page).toHaveURL(new RegExp(`/\\?agent=${analyst.handle}$`));
  const start = page.getByTestId("chat-home-start");
  await expect(start).toHaveAttribute("data-agent", analyst.handle);
  await expect(page.getByTestId("chat-home-start-title")).toHaveText("Chat with Analyst");
  await expect(start.getByTestId("chat-home-starter")).toHaveText(STARTERS);

  // The composer sits in the middle of the main pane.
  const frame = (await pane(page).boundingBox())!;
  const composer = (await page.getByTestId("chat-home-composer").boundingBox())!;
  expect(Math.abs(composer.x + composer.width / 2 - (frame.x + frame.width / 2))).toBeLessThan(24);
  const middle = composer.y + composer.height / 2;
  expect(middle).toBeGreaterThan(frame.y + frame.height * 0.25);
  expect(middle).toBeLessThan(frame.y + frame.height * 0.75);

  // A starter fills the box and leaves the sending to the member; Enter sends and opens the direct chat.
  await start.getByTestId("chat-home-starter").first().click();
  const text = page.getByTestId("chat-home-composer-text");
  await expect(text).toHaveValue(STARTERS[0]);
  await expect(text).toBeFocused();
  await text.press("Enter");
  await expect(page.getByTestId("agent-chat")).toHaveAttribute("data-frame", "chat");
  const opened = await openDirectByApi(me, analyst.handle);
  await expect(page.getByTestId("chat-view")).toHaveAttribute("data-chat-id", String(opened.id));
  await expect(page.getByTestId("chat-message").filter({ hasText: STARTERS[0] })).toBeVisible();
  const sent = (await chatPageOf(me, opened.id)).messages;
  expect(sent.map((message) => message.body)).toEqual([STARTERS[0]]);

  // Analyst is now the agent written to last: Home opens on it. The rail holds the one picker and the way to Today; the
  // chat's head names the agent without a picker of its own.
  await open(page, "/");
  await expect(picker(page)).toHaveAttribute("data-agent", analyst.handle);
  await expect(page.getByTestId("chat-view")).toHaveAttribute("data-chat-id", String(opened.id));
  await expect(rail(page).getByTestId("chat-home-picker")).toHaveAttribute("data-agent", analyst.handle);
  await expect(page.getByTestId("chat-header").getByTestId("chat-home-picker")).toHaveCount(0);
  await expect(rail(page).getByTestId("chat-home-today")).toBeVisible();
  await expect(picker(page)).toHaveCount(1);
});

test("an agent's interactive answer shows in its message on Home", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const analyst = await agentOf(me, project, "Analyst", STARTERS);

  // The project's default agent comes before Analyst in the list: the address names Analyst.
  await open(page, `/?agent=${analyst.handle}`);
  await expect(picker(page)).toHaveAttribute("data-agent", analyst.handle);
  const text = page.getByTestId("chat-home-composer-text");
  await text.fill("Compare the plans in a chart");
  await page.getByTestId("chat-home-composer-send").click();
  await expect(page.getByTestId("chat-view")).toBeVisible();

  const chat = await openDirectByApi(me, analyst.handle);
  const [asked] = (await chatPageOf(me, chat.id)).messages;
  const runId = asked.mentions[0]?.run_id;
  expect(runId, "the direct message hands the agent a run").toBeTruthy();
  await claimChatRun(analyst.live, runId!);
  await takeInbox(analyst.live, runId!);
  const [answer] = await sendOutputs(analyst.live, runId!, [
    { name: "compare.html", media_type: "text/html", data: Buffer.from(ANSWER_PAGE, "utf8"), interactive: true, caption: "Plans compared" },
  ]);
  await agentSays(analyst.live, runId!, "The second plan ships two weeks sooner; the chart compares them.");

  const shown = page.locator(`[data-testid="interactive-answer"][data-output-id="${answer.id}"]`);
  await expect(shown).toHaveAttribute("data-state", "ready", { timeout: 20_000 });
  await expect(page.frameLocator(`[data-output-id="${answer.id}"] iframe`).locator("#out")).toHaveText("The second plan ships two weeks sooner.");
});

test("with no agent to chat with, Home says why and leads to the projects", async ({ page, member }) => {
  await member([{ role: "reader", maxLevel: "internal" }]);
  await open(page, "/");
  const none = page.getByTestId("chat-home-none");
  await expect(none).toContainText("No agent to chat with yet");
  await expect(none).toContainText("worker pool");
  const projects = none.getByTestId("chat-home-projects");
  await expect(projects).toHaveAttribute("href", "/today#projects");
  await expectNoSeriousViolations(page, "Home with no agent");
  await projects.click();
  await expect(page).toHaveURL(/\/today#projects$/);
  await expect(page.locator("#main").getByTestId("home-projects")).toBeVisible();
});

test("home rail lists the agent's tasks", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const system = await workSystem(me, project);
  const asks = await seedWork(me, system, "Refund backlog");
  const running = await seedWork(me, system, "Invoice cleanup", { decide: false });
  const done = await seedWork(me, system, "Release notes", { decide: false });
  await finishWork(system, done);
  await expect.poll(async () => (await listedWork(me, done.workChat))?.status).toBe("done");

  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, `/?agent=${system.lead}`);
  // No top bar and no sidebar: the rail is Home's.
  await expect(rail(page)).toHaveAttribute("data-agent", system.lead);
  await expect(page.getByTestId("top-bar")).toHaveCount(0);
  await expect(page.locator('[data-slot="sidebar-container"]')).toHaveCount(0);
  for (const testId of ["home-menu", "home-new-task", "home-rail-search", "home-rail-direct", "palette-trigger", "inbox-bell", "chat-home-today", "user-menu"]) {
    await expect(rail(page).getByTestId(testId), testId).toBeVisible();
  }

  // A decision for the member puts its task in Needs you; the work under way is Running; the done one, this week.
  await expect(rail(page).getByTestId("home-task")).toHaveCount(3);
  const needs = rail(page).getByTestId("home-rail-needsYou");
  await expect(needs.getByRole("heading", { level: 2 })).toHaveText("Needs you");
  await expect(needs.getByTestId("home-task")).toHaveCount(1);
  await expect(taskRow(page, asks.workChat)).toHaveAttribute("data-status", "needs_person");
  await expect(taskRow(page, asks.workChat).getByTestId("home-task-title")).toHaveText("Refund backlog");
  await expect(taskRow(page, asks.workChat).getByTestId("home-task-line")).toHaveText(`Needs a person: Waiting for ${me.login} to decide`);
  await expect(taskRow(page, running.workChat)).toHaveAttribute("data-section", "running");
  await expect(taskRow(page, done.workChat)).toHaveAttribute("data-section", "thisWeek");
  await expect(taskRow(page, done.workChat).getByTestId("home-row-unread")).toHaveCount(0);
  // The chats the member asked in have the agent too: the rail lists them under the tasks.
  const groups = rail(page).getByTestId("home-group");
  await expect(groups).toHaveCount(3);
  await expect(rail(page).locator(`[data-testid="home-group"][data-chat-id="${asks.rootChat}"]`)).toContainText("Ask Refund backlog");

  // The search finds tasks by their title.
  const search = rail(page).getByTestId("home-rail-search");
  await search.fill("invoice");
  await expect(rail(page).getByTestId("home-task")).toHaveCount(1);
  await expect(taskRow(page, running.workChat)).toBeVisible();
  await search.fill("nothing like it");
  await expect(rail(page).getByTestId("home-rail-empty")).toHaveText("No task matches “nothing like it”");
  await search.fill("");

  // Another agent has other tasks: the project's default agent has none.
  await picker(page).click();
  await page.getByTestId("chat-home-picker-menu").locator(`[data-testid="chat-home-agent"][data-agent="${project}"]`).click();
  await expect(page).toHaveURL(new RegExp(`/\\?agent=${project}$`));
  await expect(rail(page).getByTestId("home-task")).toHaveCount(0);
  await expect(rail(page).getByTestId("home-rail-empty")).toBeVisible();

  // The hub's menu is the sidebar as a sheet, from its button or Ctrl or Cmd + B.
  await page.keyboard.press("ControlOrMeta+b");
  const sheet = page.getByRole("dialog");
  await expect(sheet.getByTestId("nav-today")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toBeHidden();
  await rail(page).getByTestId("home-menu").click();
  await sheet.getByTestId("nav-today").click();
  await expect(page).toHaveURL(/\/today$/);
  await expect(page.getByTestId("top-bar")).toBeVisible();
});

test("task view", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const system = await workSystem(me, me.projects[0]);
  const work = await seedWork(me, system, "Refund backlog", { decide: false });
  // @lead records the work done short of its done-when, with what is left, then the work ends.
  await setWork(system, work, { outcome: "not_met", left: "Two refunds wait on finance at lumenops.example." });
  await finishWork(system, work);

  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, `/?agent=${system.lead}&task=${work.workChat}`);
  const head = pane(page).getByTestId("chat-header");
  await expect(head).toHaveAttribute("data-view", "task");
  await expect(head).toHaveAttribute("data-status", "done");
  // No chat head bar: a source line, then the title as the page's heading.
  await expect(pane(page).getByTestId("chat-head")).toHaveCount(0);
  await expect(head.getByTestId("task-source")).toContainText(system.name);
  await expect(head.getByTestId("task-source-link")).toHaveAttribute("href", `/chats/${work.rootChat}?message=${work.root}`);
  await expect(head.getByTestId("chat-details-open")).toBeVisible();
  await expect(head.getByRole("heading", { level: 1 })).toHaveText("Refund backlog");
  await expect(head.getByTestId("task-left")).toContainText("Two refunds wait on finance");
  await expect(head.getByTestId("work-card-runs")).toBeVisible();
  await expect(head.getByTestId("task-goal")).toHaveText("Every refund ticket of Northwind has a reply.");
  await expect(head.getByTestId("task-done-when")).toContainText("No refund ticket is left without a reply.");
  await expect(head.getByTestId("task-not-met")).toBeVisible();

  // The team: the coordinator first, then the agent it asked, with its result.
  const team = head.getByTestId("task-team-member");
  await expect(team).toHaveCount(2);
  await expect(team.nth(0)).toHaveAttribute("data-agent", system.lead);
  await expect(team.nth(0)).toHaveAttribute("data-coordinator", "true");
  await expect(team.nth(1)).toHaveAttribute("data-agent", system.helper);
  await expect(team.nth(1).getByTestId("task-team-result")).toHaveText("The refund replies are drafted.");

  // The hand-off is a card: the brief, the agent's updates with the hub's lines folded, and its result.
  const handOff = pane(page).locator(`[data-testid="task-delegation"][data-agent="${system.helper}"]`);
  await expect(handOff).toHaveCount(1);
  await expect(handOff.getByTestId("task-delegation-brief")).toContainText("Please draft the refund replies for the open tickets.");
  const result = handOff.getByTestId("task-message").filter({ hasText: "The refund replies are drafted." });
  await expect(result.getByTestId("task-message-label")).toBeVisible();
  const notices = pane(page).getByTestId("task-notices").first();
  await expect(notices.getByTestId("task-notices-toggle")).toHaveAttribute("aria-expanded", "false");
  await expect(notices.getByTestId("chat-message")).toHaveCount(0);
  await notices.getByTestId("task-notices-toggle").click();
  await expect(notices.getByTestId("task-notices-toggle")).toHaveAttribute("aria-expanded", "true");
  await expect(notices.getByTestId("chat-message").first()).toBeVisible();

  // The coordinator's final answer carries its label.
  const answer = pane(page).getByTestId("task-conversation").locator(':scope > [data-testid="task-message"]').filter({ hasText: "Every refund ticket has a reply." });
  await expect(answer.getByTestId("task-message-label")).toBeVisible();
  await expectNoSeriousViolations(page, "a task's view on Home");

  // Mark as handled: the outcome no longer reads not met, what is left stays, and who marked it shows.
  await head.getByTestId("task-mark-handled").click();
  await expect(head.getByTestId("task-mark-handled")).toHaveCount(0);
  await expect(head.getByTestId("task-not-met")).toHaveCount(0);
  await expect(head.getByTestId("task-handled")).toContainText(me.login);
  await expect(head.getByTestId("task-left")).toContainText("Two refunds wait on finance");
  await expect.poll(async () => (await listedWork(me, work.workChat))?.outcome ?? null).toBeNull();
});

test("a task opens on home and survives a reload", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const system = await workSystem(me, me.projects[0]);
  const work = await seedWork(me, system, "Refund backlog");

  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, `/?agent=${system.lead}`);
  await taskRow(page, work.workChat).click();
  await expect(page).toHaveURL(new RegExp(`/\\?agent=${system.lead}&task=${work.workChat}$`));
  await expect(pane(page)).toHaveAttribute("data-task", String(work.workChat));
  await expect(pane(page).getByTestId("chat-header")).toContainText("Refund backlog");
  await expect(taskRow(page, work.workChat)).toHaveAttribute("aria-current", "page");
  // Both panes show on a wide screen: the rail stays beside the task.
  await expect(rail(page)).toBeVisible();

  await page.reload();
  await expect(pane(page)).toHaveAttribute("data-task", String(work.workChat));
  await expect(pane(page).getByTestId("chat-header")).toContainText("Refund backlog");
  await expect(taskRow(page, work.workChat)).toHaveAttribute("aria-current", "page");

  // The direct chat leaves the task; New task opens it with its box focused.
  await rail(page).getByTestId("home-rail-direct").click();
  await expect(page).toHaveURL(new RegExp(`/\\?agent=${system.lead}$`));
  await expect(page.getByTestId("agent-chat")).toHaveAttribute("data-agent", system.lead);
  await expect(rail(page).getByTestId("home-rail-direct")).toHaveAttribute("aria-current", "page");
  await taskRow(page, work.workChat).click();
  await rail(page).getByTestId("home-new-task").click();
  await expect(page).toHaveURL(new RegExp(`/\\?agent=${system.lead}$`));
  await expect(page.getByTestId("chat-home-composer-text")).toBeFocused();
});

for (const scheme of ["light", "dark"] as const) {
  test(`Home passes axe at 1440 and 375 px in ${scheme}, on its rail, a task, the start and the chat, and never scrolls sideways`, async ({ page, member }) => {
    test.slow(); // two widths, five views each, axe on every one
    await page.emulateMedia({ colorScheme: scheme });
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    const analyst = await agentOf(me, project, "Analyst", STARTERS);
    const scribe = await agentOf(me, project, "Scribe");
    const direct = await openDirectByApi(me, scribe.handle);
    await sendByApi(me, direct.id, "Draft the release notes");
    const system = await workSystem(me, project);
    const work = await seedWork(me, system, "Refund backlog");
    for (const [width, height] of [
      [1440, 900],
      [375, 812],
    ] as const) {
      const phone = width < 768;
      await page.setViewportSize({ width, height });
      await open(page, `/?agent=${system.lead}`);
      await expect(taskRow(page, work.workChat)).toBeVisible();
      await noSidewaysScroll(page);
      await expectNoSeriousViolations(page, `Home's rail with a task at ${width} px in ${scheme}`);

      await open(page, `/?agent=${system.lead}&task=${work.workChat}`);
      await expect(pane(page).getByTestId("chat-header")).toContainText("Refund backlog");
      await noSidewaysScroll(page);
      await expectNoSeriousViolations(page, `a task open on Home at ${width} px in ${scheme}`);

      await open(page, `/?agent=${scribe.handle}`);
      if (phone) await rail(page).getByTestId("home-rail-direct").click();
      await expect(page.getByTestId("chat-message").filter({ hasText: "Draft the release notes" })).toBeVisible();
      await noSidewaysScroll(page);
      await expectNoSeriousViolations(page, `Home in a chat at ${width} px in ${scheme}`);

      await open(page, `/?agent=${analyst.handle}`);
      if (phone) await rail(page).getByTestId("home-rail-direct").click();
      const start = page.getByTestId("chat-home-start");
      await expect(start.getByTestId("chat-home-starter")).toHaveCount(STARTERS.length);
      for (const starter of await start.getByTestId("chat-home-starter").all()) {
        expect((await starter.boundingBox())!.height, "a starter is a 44 px touch target").toBeGreaterThanOrEqual(44);
      }
      await noSidewaysScroll(page);
      await expectNoSeriousViolations(page, `Home on an agent's start at ${width} px in ${scheme}`);
      if (phone) await pane(page).getByTestId("home-back").click();
      await picker(page).click();
      await expect(page.getByTestId("chat-home-picker-menu")).toBeVisible();
      await expectNoSeriousViolations(page, `Home with the agent picker open at ${width} px in ${scheme}`);
      await page.keyboard.press("Escape");
    }
  });
}
