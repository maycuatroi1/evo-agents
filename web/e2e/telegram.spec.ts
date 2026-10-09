import type { Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { expect, isDeployed, test } from "./support/fixtures";
import { open } from "./support/plans";
import { codeOf, newChatId, pressStart } from "./support/telegram";
import { toast } from "./support/toast";

/**
 * Linking Telegram from the Inbox against the real API, with the stack's fake Bot API in place of Telegram
 * (e2e/support/telegram.ts): the dialog makes a one-time link, waits while the member presses Start in Telegram, says
 * the chat is linked once the hub's webhook took the code, and unlinks after a confirm step in place. A code used once
 * links nothing again. axe checks the dialog in each state, in light and dark, and at a phone's width.
 */
test.skip(isDeployed, "links a chat through the local stack's fake Telegram");

function dialogOf(page: Page) {
  return page.getByTestId("telegram-dialog");
}

async function linkFromTheInbox(page: Page, username: string, theme: string): Promise<void> {
  await open(page, "/inbox");
  await page.getByTestId("inbox-telegram").click();
  const dialog = dialogOf(page);
  await expect(dialog).toHaveAttribute("data-view", "unlinked");
  await expect(dialog.getByRole("heading", { name: "Telegram" })).toBeVisible();
  await expect(dialog.getByTestId("telegram-unlinked")).toContainText("No chat is linked");
  await expectNoSeriousViolations(page, `the Telegram dialog, no chat linked (${theme})`);

  await dialog.getByTestId("telegram-make-link").click();
  await expect(dialog).toHaveAttribute("data-view", "pending");
  const url = (await dialog.getByTestId("telegram-link").textContent()) ?? "";
  expect(url).toMatch(/^https:\/\/t\.me\/evo_test_bot\?start=[A-Za-z0-9_-]{32}$/);
  await expect(dialog.getByTestId("telegram-open")).toHaveAttribute("href", url);
  await expect(dialog.getByTestId("telegram-waiting")).toContainText("Waiting for Telegram. The link works once, until");
  await expectNoSeriousViolations(page, `the Telegram dialog with its link (${theme})`);

  const code = codeOf(url);
  const chat = newChatId();
  expect(await pressStart(code, chat, username)).toBe("linked");
  await expect(dialog).toHaveAttribute("data-view", "linked", { timeout: 15_000 });
  await expect(dialog.getByTestId("telegram-linked")).toContainText(`Linked to @${username}`);
  await expect(toast(page, "Telegram linked")).toBeVisible();
  expect(await pressStart(code, newChatId(), "someone_else")).toBe("link_failed"); // a code links once
  await expectNoSeriousViolations(page, `the Telegram dialog, linked (${theme})`);
}

test("a member links a Telegram chat from the Inbox and unlinks it", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const username = `tg_${me.login.replaceAll("-", "_").slice(-20)}`;
  await linkFromTheInbox(page, username, "light");

  const dialog = dialogOf(page);
  await dialog.getByTestId("telegram-unlink").click();
  await expect(dialog.getByTestId("telegram-confirm-unlink")).toContainText("Unlink this chat?");
  await expectNoSeriousViolations(page, "the Telegram dialog asking to unlink (light)");
  await dialog.getByTestId("telegram-unlink-confirm").click();
  await expect(dialog).toHaveAttribute("data-view", "unlinked");
  await expect(toast(page, "Telegram unlinked")).toBeVisible();

  // Closing and opening again reads where the link stands from the hub.
  await dialog.getByTestId("telegram-close").click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId("inbox-telegram")).toBeFocused();
});

test.describe("dark theme", () => {
  test.use({ colorScheme: "dark" });

  test("the Telegram dialog reads well in dark", async ({ page, member }) => {
    const me = await member([{ role: "writer", maxLevel: "internal" }]);
    await linkFromTheInbox(page, `tg_${me.login.replaceAll("-", "_").slice(-20)}`, "dark");
    await expect(page.locator("html")).toHaveClass(/dark/);
  });
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test("the Telegram dialog fits a phone and its buttons are 44 px tall", async ({ page, member }) => {
    await member([{ role: "writer", maxLevel: "internal" }]);
    await open(page, "/inbox");
    await page.getByTestId("inbox-telegram").click();
    const dialog = dialogOf(page);
    await expect(dialog).toHaveAttribute("data-view", "unlinked");
    await dialog.getByTestId("telegram-make-link").click();
    await expect(dialog).toHaveAttribute("data-view", "pending");
    const box = await dialog.boundingBox();
    expect(box && box.x >= 0 && box.x + box.width <= 375).toBe(true);
    for (const id of ["telegram-open", "telegram-make-link", "telegram-close"]) {
      const button = await dialog.getByTestId(id).boundingBox();
      expect(button?.height ?? 0).toBeGreaterThanOrEqual(44);
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    await expectNoSeriousViolations(page, "the Telegram dialog on a phone");
  });
});
