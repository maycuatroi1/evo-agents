import path from "node:path";

import type { Locator, Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { agentAsks, agentResumes, authorWorker, dispatchAuthor, ensureAuthorSkill, startAuthor } from "./support/author";
import { agentSays, chatPageOf, chatPath, chatWorker, claimChatRun, createChatByApi, openDirectByApi, sendByApi, takeInbox } from "./support/chats";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { sendOutputs } from "./support/outputs";
import { open } from "./support/plans";
import { runPath, seedRunPlan } from "./support/runs";

/**
 * A text document of a chat read beside it (web/DESIGN.md, Chats; components/data/doc-panel.tsx), against the real
 * API: a file attached to a message has "Open NAME beside the chat" under it, which opens the panel beside the chat
 * from lg, the composer still in reach, and in a sheet on a phone. The same name sent again, by a person or by an
 * agent's run, is the document's next version: the open panel moves to it by itself, says so, and shows it Marked (the
 * words added in green, those removed in red and struck through) against the version shown; By line shows the lines.
 * Reloaded with the first version out of the page's messages, the document still opens Marked against it (the hub
 * lists every version). A YAML file of an author run's Chat tab opens in the same panel, By line. Close gives the
 * focus back to the button. With E2E_SCREENSHOT_DIR set it also takes review screenshots, light and dark. Pages render
 * in English.
 */
test.skip(isDeployed, "uploads files through the local stack");

const dir = process.env.E2E_SCREENSHOT_DIR;
const FIRST = "# Release plan\n\n- Freeze main on Monday.\n- Tag the release candidate.\n- Write the notes.\n";
const FIXED = "# Release plan\n\n- Freeze main on Monday.\n- Tag the release candidate on Tuesday.\n- Write the notes.\n- Announce it.\n";
const MOVED = "# Release plan\n\n- Freeze main on Tuesday.\n- Tag the release candidate.\n- Write the notes.\n";
const DEPLOY = "name: web\nreplicas: 2\nimage: hub:0.53\n";
const SCALED = "name: web\nreplicas: 3\nimage: hub:0.54\n";
/** Messages a chat's page reads at first (`PAGE_SIZE` of chat-queries.ts): more push the first version out of it. */
const PAGE = 50;

function main(page: Page) {
  return page.locator("#main");
}

/** Sends a message with `plan.md` holding `text`, attached with the composer's attach button. */
async function sendPlan(page: Page, text: string, body: string) {
  const composer = main(page).getByTestId("chat-composer");
  const chooser = page.waitForEvent("filechooser");
  await composer.getByTestId("chat-composer-attach").click();
  await (await chooser).setFiles([{ name: "plan.md", mimeType: "text/markdown", buffer: Buffer.from(text) }]);
  await expect(composer.locator('[data-testid="attachment-chip"][data-status="done"]')).toHaveCount(1);
  await composer.getByTestId("chat-composer-text").fill(body);
  await composer.getByTestId("chat-composer-text").press("Enter");
  await expect(composer.getByTestId("attachment-chip")).toHaveCount(0);
}

function sent(page: Page, body: string): Locator {
  return main(page).getByTestId("chat-log").getByTestId("chat-message").filter({ hasText: body });
}

async function shoot(page: Page, name: string) {
  if (!dir) return;
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await page.screenshot({ path: path.join(dir, `chat-doc-${name}-${scheme}.png`) });
  }
  await page.emulateMedia({ colorScheme: "light" });
}

test("a Markdown file opens beside the chat, moves to the next version Marked, and opens Marked again after a reload", async ({ page, member }) => {
  test.slow();
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const chat = await createChatByApi(me, { title: uniqueName("Release docs") });
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, chatPath(chat.id));

  await sendPlan(page, FIRST, "First draft of the plan.");
  const button = sent(page, "First draft of the plan.").getByTestId("message-docs").getByRole("button", { name: "Open plan.md beside the chat" });
  await button.click();
  const panel = main(page).getByTestId("doc-panel");
  await expect(panel).toBeFocused();
  await expect(panel.getByRole("heading", { name: "plan.md" })).toBeVisible();
  await expect(panel.getByTestId("doc-markdown")).toContainText("Tag the release candidate.");
  // Beside the chat: the panel's left edge is right of the chat's.
  const chatBox = (await main(page).locator("section[aria-labelledby=chat-title]").boundingBox())!;
  const panelBox = (await panel.boundingBox())!;
  expect(panelBox.x).toBeGreaterThan(chatBox.x + chatBox.width - 1);
  await expect(panel.getByTestId("doc-mode-tracked")).toHaveCount(0);
  await expect(panel.getByTestId("doc-mode-lines")).toHaveCount(0);
  await shoot(page, "read");

  // A page of messages comes between the two versions (so a reload reads the second only), as the panel stays open.
  for (let index = 0; index < PAGE; index++) await sendByApi(me, chat.id, `Note ${index + 1}`);
  await expect(sent(page, `Note ${PAGE}`)).toHaveCount(1);
  await expect(panel.getByTestId("doc-markdown")).toContainText("Tag the release candidate.");

  // The composer is in reach with the panel open: the fixed file under the same name is the next version, Marked.
  await sendPlan(page, FIXED, "Fixed the dates.");
  await expect(panel.getByTestId("doc-reloaded")).toContainText("sent a new version: reloaded, the changes show below.");
  await expect(panel).toHaveAttribute("data-mode", "tracked");
  const tracked = panel.getByTestId("doc-tracked");
  await expect(tracked.locator('ins[data-tracked="add"]').filter({ hasText: "Tuesday" })).toHaveCount(1);
  await expect(tracked.locator('ins[data-tracked="add"]').filter({ hasText: "Announce it." })).toHaveCount(1);
  await expect(tracked.getByRole("listitem").filter({ hasText: "Write the notes." })).toHaveCount(1);
  await expect(panel.getByTestId("doc-diff-legend")).toContainText("Added");
  await expect(panel.getByTestId("doc-stats")).toHaveText("+2 −1");
  await expect(panel.getByTestId("doc-version").locator("option")).toHaveCount(2);
  await expectNoSeriousViolations(page, "chat with a document's changes marked");
  await shoot(page, "tracked");

  await panel.getByTestId("doc-mode-lines").click();
  const diff = panel.getByTestId("doc-diff");
  await expect(diff.locator('[data-kind="add"]')).toHaveCount(2);
  await expect(diff.locator('[data-kind="del"]')).toHaveCount(1);
  await expect(diff.locator('[data-kind="del"]')).toContainText("Removed: - Tag the release candidate.");
  await expect(diff.locator('[data-kind="add"] [data-word="add"]').first()).toContainText("on Tuesday");
  await shoot(page, "lines");

  await panel.getByTestId("doc-mode-read").click();
  await expect(panel.getByTestId("doc-markdown")).toContainText("Announce it.");
  await panel.getByTestId("doc-close").click();
  await expect(panel).toHaveCount(0);
  await expect(button).toBeFocused();

  // A page of messages after the first version pushed it out of what the page reads after a reload.
  await page.reload();
  await expect(sent(page, "Fixed the dates.")).toHaveCount(1);
  await expect(sent(page, "First draft of the plan.")).toHaveCount(0);
  await sent(page, "Fixed the dates.").getByTestId("message-docs").getByRole("button", { name: "Open plan.md beside the chat" }).click();
  await expect(panel).toHaveAttribute("data-mode", "tracked");
  await expect(panel.getByTestId("doc-tracked").locator('ins[data-tracked="add"]').filter({ hasText: "Announce it." })).toHaveCount(1);
  await expect(panel.getByTestId("doc-version").locator("option")).toHaveCount(2);
  await expect(panel.getByTestId("doc-base").locator("option")).toHaveCount(1);
  await expect(panel.getByTestId("doc-base").locator("option")).toContainText("Version 1");
});

test("an agent sends back the file it fixed: the open panel moves to it, the words removed red and struck through", async ({ page, member }) => {
  test.slow();
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const chat = await openDirectByApi(me, project);
  const live = await chatWorker(me, project, uniqueName("doc-desk"));
  await page.setViewportSize({ width: 1440, height: 900 });
  await open(page, chatPath(chat.id));

  await sendPlan(page, FIRST, "Move the freeze to Tuesday.");
  const asked = (await chatPageOf(me, chat.id)).messages.find((message) => message.body === "Move the freeze to Tuesday.");
  const runId = asked?.mentions[0]?.run_id;
  if (!runId) throw new Error(`the message made no run: ${JSON.stringify(asked?.mentions)}`);
  await sent(page, "Move the freeze to Tuesday.").getByTestId("message-docs").getByRole("button", { name: "Open plan.md beside the chat" }).click();
  const panel = main(page).getByTestId("doc-panel");
  await expect(panel.getByTestId("doc-markdown")).toContainText("Freeze main on Monday.");

  // The agent's run sends the file back under its name (`evo-agents worker output`), then answers.
  await claimChatRun(live, runId);
  await takeInbox(live, runId);
  await sendOutputs(live, runId, [{ name: "plan.md", media_type: "text/markdown", data: Buffer.from(MOVED) }]);
  await agentSays(live, runId, "Moved the freeze to Tuesday.");
  await expect(panel.getByTestId("doc-reloaded")).toContainText("sent a new version");
  await expect(panel).toHaveAttribute("data-mode", "tracked");
  const tracked = panel.getByTestId("doc-tracked");
  const removed = tracked.locator('del[data-tracked="del"]').filter({ hasText: "Monday" });
  await expect(removed).toHaveCount(1);
  await expect(removed).toHaveCSS("text-decoration-line", "line-through");
  await expect(tracked.locator('ins[data-tracked="add"]').filter({ hasText: "Tuesday" })).toHaveCount(1);
  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    await expectNoSeriousViolations(page, `a document an agent fixed, marked, ${scheme}`);
  }
  await page.emulateMedia({ colorScheme: "light" });
  await shoot(page, "agent");
});

test("a YAML file of an author run's Chat tab opens in the panel By line, the words that changed tinted", async ({ page, member }) => {
  test.slow();
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await ensureAuthorSkill();
  await seedRunPlan(me, project);
  const live = await authorWorker(me, project, uniqueName("desk"));
  const run = await dispatchAuthor(me, project, live.worker.id, "Tune the deploy file.");
  await startAuthor(live, run.id);
  await agentAsks(live, run.id, "Send me the deploy file.");
  await open(page, `${runPath(project, run.id)}?tab=chat`);
  const chat = main(page).getByTestId("run-chat");
  await expect(chat.getByTestId("run-chat-status")).toHaveText("Waiting for you");

  const reply = chat.getByTestId("run-chat-reply");
  const chooser = page.waitForEvent("filechooser");
  await reply.getByTestId("run-chat-reply-attach").click();
  await (await chooser).setFiles([{ name: "deploy.yaml", mimeType: "application/yaml", buffer: Buffer.from(DEPLOY) }]);
  await expect(reply.locator('[data-testid="attachment-chip"][data-status="done"]')).toHaveCount(1);
  await chat.getByRole("textbox", { name: "Reply to the agent" }).fill("Here it is.");
  await chat.getByRole("textbox", { name: "Reply to the agent" }).press("Control+Enter");
  await expect(chat.getByTestId("run-chat-reply-sent")).toHaveText("Sent.");
  await agentResumes(live, run.id);
  await sendOutputs(live, run.id, [{ name: "deploy.yaml", media_type: "application/yaml", data: Buffer.from(SCALED) }]);

  const button = chat.getByTestId("run-chat-owner").getByTestId("run-chat-docs").getByRole("button", { name: "Open deploy.yaml beside the chat" });
  await button.click();
  const sheet = page.getByTestId("run-doc-sheet");
  const panel = sheet.getByTestId("doc-panel");
  await expect(panel).toHaveAttribute("data-mode", "lines");
  await expect(panel.getByTestId("doc-mode-tracked")).toHaveCount(0);
  const diff = panel.getByTestId("doc-diff");
  await expect(diff.locator('[data-kind="del"]')).toHaveCount(2);
  await expect(diff.locator('[data-kind="add"]')).toHaveCount(2);
  await expect(diff.locator('[data-kind="del"] [data-word="del"]').first()).toHaveText("2");
  await expect(diff.locator('[data-kind="add"] [data-word="add"]').first()).toHaveText("3");
  await expect(diff.locator('[data-kind="del"]').first().locator("> span").last()).toHaveCSS("text-decoration-line", "line-through");
  await panel.getByTestId("doc-mode-read").click();
  await expect(panel.getByTestId("doc-text")).toContainText("replicas: 3");
  await expectNoSeriousViolations(page, "a YAML document of a run's chat");
  await shoot(page, "run-yaml");
  await panel.getByTestId("doc-close").click();
  await expect(sheet).toHaveCount(0);
  await expect(button).toBeFocused();
});

test("on a phone a document of two versions opens Marked in a sheet, nothing scrolls sideways, and Close gives the focus back", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const chat = await createChatByApi(me, { title: uniqueName("Phone docs") });
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, chatPath(chat.id));

  await sendPlan(page, FIRST, "Plan for the phone.");
  await sendPlan(page, FIXED, "Plan fixed for the phone.");
  const button = sent(page, "Plan fixed for the phone.").getByTestId("message-docs").getByRole("button", { name: "Open plan.md beside the chat" });
  await button.click();
  const sheet = page.getByRole("dialog", { name: "Document" });
  await expect(sheet).toHaveAttribute("data-testid", "chat-doc-sheet");
  const panel = sheet.getByTestId("doc-panel");
  await expect(panel).toHaveAttribute("data-mode", "tracked");
  await expect(panel.getByTestId("doc-tracked")).toContainText("Freeze main on Monday.");
  const box = (await panel.boundingBox())!;
  expect(box.width).toBeGreaterThan(370);
  expect(box.height).toBeGreaterThan(700);
  const sideways = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(sideways).toBeLessThanOrEqual(0);
  await expectNoSeriousViolations(page, "a document in its sheet on a phone");
  await shoot(page, "phone");
  await panel.getByTestId("doc-close").click();
  await expect(sheet).toHaveCount(0);
  await expect(button).toBeFocused();
});
