import path from "node:path";

import type { Locator, Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { chatPath, createChatByApi } from "./support/chats";
import { expect, isDeployed, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";

/**
 * A Markdown document of a chat read beside it (web/DESIGN.md, Chats; doc-panel.tsx), against the real API: a file
 * attached to a message has "Open NAME beside the chat" under it, which opens the panel beside the chat from lg, the
 * composer still in reach, and in a sheet on a phone. The same name sent again is the document's next version: the open
 * panel reloads by itself, says so, and shows the lines added and removed. Close gives the focus back to the button.
 * With E2E_SCREENSHOT_DIR set it also takes review screenshots, light and dark. Pages render in English.
 */
test.skip(isDeployed, "uploads files through the local stack");

const dir = process.env.E2E_SCREENSHOT_DIR;
const FIRST = "# Release plan\n\n- Freeze main on Monday.\n- Tag the release candidate.\n- Write the notes.\n";
const FIXED = "# Release plan\n\n- Freeze main on Monday.\n- Tag the release candidate on Tuesday.\n- Write the notes.\n- Announce it.\n";

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

test("a Markdown file opens beside the chat, reloads with its changes when sent again, and Close gives the focus back", async ({ page, member }) => {
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
  await expect(panel.getByTestId("doc-mode-changes")).toHaveCount(0);
  await shoot(page, "read");

  // The composer is in reach with the panel open: the fixed file under the same name is the next version.
  await sendPlan(page, FIXED, "Fixed the dates.");
  await expect(panel.getByTestId("doc-reloaded")).toContainText("sent a new version: reloaded, the changes show below.");
  const diff = panel.getByTestId("doc-diff");
  await expect(diff.locator('[data-kind="add"]')).toHaveCount(2);
  await expect(diff.locator('[data-kind="del"]')).toHaveCount(1);
  await expect(diff.locator('[data-kind="del"]')).toContainText("Removed: - Tag the release candidate.");
  await expect(panel.getByTestId("doc-stats")).toHaveText("+2 −1");
  await expect(panel.getByTestId("doc-version").locator("option")).toHaveCount(2);
  await expectNoSeriousViolations(page, "chat with a document's changes");
  await shoot(page, "changes");

  await panel.getByTestId("doc-mode-read").click();
  await expect(panel.getByTestId("doc-markdown")).toContainText("Announce it.");
  await panel.getByTestId("doc-close").click();
  await expect(panel).toHaveCount(0);
  await expect(button).toBeFocused();
});

test("on a phone the document opens in a sheet, nothing scrolls sideways, and Close gives the focus back", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const chat = await createChatByApi(me, { title: uniqueName("Phone docs") });
  await page.setViewportSize({ width: 375, height: 812 });
  await open(page, chatPath(chat.id));

  await sendPlan(page, FIRST, "Plan for the phone.");
  const button = sent(page, "Plan for the phone.").getByTestId("message-docs").getByRole("button", { name: "Open plan.md beside the chat" });
  await button.click();
  const sheet = page.getByRole("dialog", { name: "Document" });
  await expect(sheet).toHaveAttribute("data-testid", "chat-doc-sheet");
  const panel = sheet.getByTestId("doc-panel");
  await expect(panel.getByTestId("doc-markdown")).toContainText("Freeze main on Monday.");
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
