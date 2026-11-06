import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { createAgentByApi } from "./support/agents";
import { agentSays, chatPageOf, chatWorker, claimChatRun, openDirectByApi, sendByApi, takeInbox } from "./support/chats";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { sendOutputs } from "./support/outputs";
import { open } from "./support/plans";

/**
 * The chat home (plan chat-first-home, criterion b3; docs/hub.md, Chat home): Home `/` is a chat with one of the agents
 * the member may give work to (GET /v1/me/chat-agents), the one written to last chosen first. Before a first message
 * the agent's composer sits in the middle of the frame with its starters; the first message opens the member's direct
 * chat with the agent, and the chat's own components show it, interactive answers included. With no agent the page
 * says why and leads to the projects, on Today. In light and dark, at 1440 and 375 px, axe finds nothing serious and
 * the page never scrolls sideways. The specs play the agent's worker; no model is asked anything.
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

const home = (page: Page) => page.getByTestId("chat-home");
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

  // The composer sits in the middle of the frame.
  const frame = (await home(page).boundingBox())!;
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

  // Analyst is now the agent written to last: Home opens on it.
  await open(page, "/");
  await expect(picker(page)).toHaveAttribute("data-agent", analyst.handle);
  await expect(page.getByTestId("chat-view")).toHaveAttribute("data-chat-id", String(opened.id));
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

for (const scheme of ["light", "dark"] as const) {
  test(`Home passes axe at 1440 and 375 px in ${scheme}, on the start and in the chat, and never scrolls sideways`, async ({ page, member }) => {
    test.slow(); // two widths, three views each, axe on every one
    await page.emulateMedia({ colorScheme: scheme });
    const me = await member([{ role: "admin", maxLevel: "internal" }]);
    const project = me.projects[0];
    const analyst = await agentOf(me, project, "Analyst", STARTERS);
    const scribe = await agentOf(me, project, "Scribe");
    const direct = await openDirectByApi(me, scribe.handle);
    await sendByApi(me, direct.id, "Draft the release notes");
    for (const [width, height] of [
      [1440, 900],
      [375, 812],
    ] as const) {
      await page.setViewportSize({ width, height });
      await open(page, "/");
      await expect(page.getByTestId("chat-message").filter({ hasText: "Draft the release notes" })).toBeVisible();
      await noSidewaysScroll(page);
      await expectNoSeriousViolations(page, `Home in a chat at ${width} px in ${scheme}`);

      await open(page, `/?agent=${analyst.handle}`);
      const start = page.getByTestId("chat-home-start");
      await expect(start.getByTestId("chat-home-starter")).toHaveCount(STARTERS.length);
      for (const starter of await start.getByTestId("chat-home-starter").all()) {
        expect((await starter.boundingBox())!.height, "a starter is a 44 px touch target").toBeGreaterThanOrEqual(44);
      }
      await noSidewaysScroll(page);
      await expectNoSeriousViolations(page, `Home on an agent's start at ${width} px in ${scheme}`);
      await picker(page).click();
      await expect(page.getByTestId("chat-home-picker-menu")).toBeVisible();
      await expectNoSeriousViolations(page, `Home with the agent picker open at ${width} px in ${scheme}`);
      await page.keyboard.press("Escape");
    }
  });
}
