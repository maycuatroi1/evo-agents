import { mkdtempSync, rmSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

import type { JSHandle, Locator, Page } from "@playwright/test";

import { expectNoSeriousViolations } from "./support/a11y";
import { createAgentByApi, PIXEL_PNG } from "./support/agents";
import { agentAsks, authorWorker, chatOf, dispatchAuthor, ensureAuthorSkill, startAuthor } from "./support/author";
import { chatPageOf, chatPath, createChatByApi, openDirectByApi, sendByApi } from "./support/chats";
import { expect, isDeployed, type Member, test } from "./support/fixtures";
import { uniqueName } from "./support/hub";
import { open } from "./support/plans";
import { claimRun, dispatch, liveWorker, runPath, seedRunPlan, startRun } from "./support/runs";

/**
 * Files people attach to their messages, against the real API (docs/hub.md, Attachments): in a held run's composer, the
 * reply of an author run's Chat tab, a group chat, a thread and a direct chat with an agent. A file is picked with the
 * attach button (by keyboard too), dropped on the chat (the overlay shows only while files are dragged), or pasted
 * into the text area; each is a chip with its name, size and upload, removed (the hub deletes it) or retried after a
 * failure. The web refuses a file over 50 MiB and the eleventh file before uploading, sends a file over the /v1
 * rewrite's default 10 MB body whole, waits for the uploads before sending, sends a message of files only, and keeps
 * the chips when the send fails. Sent, the files show in the chat, the thread, the author run's chat and the run's
 * trace, an image as its thumbnail, and open in the outputs' viewer. At 375 px nothing scrolls sideways. The specs
 * play the worker. Pages render in English.
 */
test.skip(isDeployed, "uploads files through the local stack");

const MIB = 1024 * 1024;
const NOTES = { name: "notes.md", mimeType: "text/markdown", buffer: Buffer.from("# Notes\n\nThe nightly build is red since Tuesday.\n") };
const SHOT = { name: "screenshot.png", mimeType: "image/png", buffer: PIXEL_PNG };
const LOG = { name: "build.log", mimeType: "text/plain", buffer: Buffer.from("step 1 ok\nstep 2 failed\n") };

type Payload = { name: string; mimeType: string; buffer: Buffer };

function main(page: Page) {
  return page.locator("#main");
}

/** A DataTransfer in the page holding `files`, as a drag from the file manager or a paste carries them; text without. */
async function transfer(page: Page, files: Payload[], text: string | null = null): Promise<JSHandle<DataTransfer>> {
  const list = files.map((file) => ({ name: file.name, type: file.mimeType, data: file.buffer.toString("base64") }));
  return page.evaluateHandle(
    ({ list, text }) => {
      const data = new DataTransfer();
      for (const file of list) data.items.add(new File([Uint8Array.from(atob(file.data), (c) => c.charCodeAt(0))], file.name, { type: file.type }));
      if (text !== null) data.setData("text/plain", text);
      return data;
    },
    { list, text },
  );
}

/** Drags `files` over `frame`: the overlay shows; then drops them. */
async function drop(page: Page, frame: Locator, files: Payload[]) {
  const dataTransfer = await transfer(page, files);
  await frame.dispatchEvent("dragenter", { dataTransfer });
  await frame.dispatchEvent("dragover", { dataTransfer });
  await expect(frame.getByTestId("file-drop-overlay")).toBeVisible();
  await expect(frame.getByTestId("file-drop-overlay")).toHaveText("Drop files to attach them");
  await frame.dispatchEvent("drop", { dataTransfer });
  await expect(frame.getByTestId("file-drop-overlay")).toHaveCount(0);
}

/** Pastes `files` into the text area, as a screenshot copied to the clipboard is. */
async function paste(page: Page, box: Locator, files: Payload[]) {
  const dataTransfer = await transfer(page, files);
  await box.evaluate((element, data) => {
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: data, bubbles: true, cancelable: true }));
  }, dataTransfer);
}

/** Picks `files` with the attach button and the system's file picker. */
async function pick(page: Page, attach: Locator, files: Payload[] | string[], press = false) {
  const chooser = page.waitForEvent("filechooser");
  if (press) await attach.press("Enter");
  else await attach.click();
  await (await chooser).setFiles(files);
}

function chip(scope: Locator, name: string) {
  return scope.locator(`[data-testid="attachment-chip"][data-name="${name}"]`);
}

/** The upload requests the page makes, so a test can say none went. */
function uploads(page: Page) {
  const seen: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname.endsWith("/attachments")) seen.push(request.url());
  });
  return seen;
}

/** The image chip loads its thumbnail from the hub, and opens the viewer on the file. */
async function opensImage(page: Page, strip: Locator, name: string) {
  const image = strip.getByRole("button", { name: new RegExp(`^Open ${name.replaceAll(".", "\\.")}, Image, `) });
  await expect(image).toHaveAttribute("data-viewer", "image");
  await expect.poll(() => image.getByTestId("output-thumb-image").evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  await image.click();
  const viewer = page.getByTestId("output-viewer");
  await expect(viewer.getByTestId("output-viewer-name")).toHaveText(name);
  await expect(viewer.getByTestId("output-viewer-meta")).toContainText("Attached to a message");
  const href = await viewer.getByTestId("output-viewer-download").getAttribute("href");
  const bytes = await page.request.get(href!);
  expect(bytes.status()).toBe(200);
  expect(Buffer.from(await bytes.body()).equals(PIXEL_PNG)).toBe(true);
  await viewer.getByTestId("output-viewer-close").click();
  await expect(viewer).toHaveCount(0);
}

/** A run of step 2 that `me` dispatched, claimed by their worker, with the agent started: it takes messages. */
async function heldRun(me: Member) {
  const project = me.projects[0];
  await seedRunPlan(me, project);
  const live = await liveWorker(me, project, uniqueName("laptop"));
  const [run] = await dispatch(me, project, ["2"]);
  expect((await claimRun(live))?.id).toBe(run.id);
  await startRun(live, run.id);
  return { project, run };
}

test("a held run's composer takes files picked, dropped and pasted, removes one, sends files only, and the trace opens them", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const { project, run } = await heldRun(me);
  await open(page, runPath(project, run.id));
  const composer = main(page).getByTestId("run-composer");
  const frame = main(page).getByTestId("run-composer-drop");
  const attach = composer.getByTestId("run-composer-attach");
  await expect(attach).toHaveAccessibleName("Attach files, at most 10");

  // Picked: a chip each, with its name and size, uploaded.
  await pick(page, attach, [LOG, NOTES]);
  await expect(chip(composer, "build.log")).toHaveAttribute("data-status", "done");
  await expect(chip(composer, "notes.md")).toHaveAttribute("data-status", "done");
  await expect(chip(composer, "build.log")).toContainText("24 bytes");
  await expect(composer.getByRole("list", { name: "Files to send" }).getByRole("listitem")).toHaveCount(2);

  // Removed: the chip goes and the hub deletes the file uploaded.
  const deleted = page.waitForResponse((response) => response.request().method() === "DELETE" && /\/attachments\/\d+$/.test(new URL(response.url()).pathname));
  await chip(composer, "build.log").getByRole("button", { name: "Remove build.log" }).click();
  expect((await deleted).status()).toBe(204);
  await expect(chip(composer, "build.log")).toHaveCount(0);

  // Text dragged over the box shows no overlay; files do, and dropped they are attached.
  const text = await transfer(page, [], "just words");
  await frame.dispatchEvent("dragenter", { dataTransfer: text });
  await expect(frame.getByTestId("file-drop-overlay")).toHaveCount(0);
  await drop(page, frame, [SHOT]);
  await expect(chip(composer, "screenshot.png")).toHaveAttribute("data-status", "done");

  // Pasted into the box: an image from the clipboard.
  await paste(page, composer.getByTestId("run-composer-text"), [{ ...SHOT, name: "pasted.png" }]);
  await expect(chip(composer, "pasted.png")).toHaveAttribute("data-status", "done");

  // Files only, no words: sent, the chips gone, and the trace shows the message with its files.
  await composer.getByTestId("run-composer-send").click();
  await expect(composer.getByTestId("run-composer-sent")).toContainText("Sent.");
  await expect(composer.getByTestId("attachment-chip")).toHaveCount(0);
  const row = main(page).getByTestId("trace").locator('[data-testid="trace-item"][data-type="user"]');
  await expect(row).toHaveCount(1);
  const strip = row.getByTestId("trace-user-files");
  await expect(strip.getByTestId("output-chip")).toHaveCount(3);
  await expect(strip.locator('[data-testid="output-chip"][data-viewer="text"]')).toHaveAccessibleName(/^Open notes\.md, Text, /);
  await opensImage(page, strip, "screenshot.png");
  // The member's files are the message's, not the run's outputs.
  await expect(main(page).getByTestId("output-tile")).toHaveCount(0);
});

test("the author run's Chat tab takes a file dropped anywhere on the chat with the reply, and shows it in the chat", async ({ page, member }) => {
  const me = await member([{ role: "writer", maxLevel: "internal" }]);
  const project = me.projects[0];
  await ensureAuthorSkill();
  await seedRunPlan(me, project);
  const live = await authorWorker(me, project, uniqueName("desk"));
  const run = await dispatchAuthor(me, project, live.worker.id, "Write a plan for chat attachments.");
  await startAuthor(live, run.id);
  await agentAsks(live, run.id, "Which screenshot shows the broken composer?");
  await open(page, `${runPath(project, run.id)}?tab=chat`);
  const chat = main(page).getByTestId("run-chat");
  await expect(chat.getByTestId("run-chat-status")).toHaveText("Waiting for you");

  await drop(page, chat, [SHOT]);
  const reply = chat.getByTestId("run-chat-reply");
  await expect(chip(reply, "screenshot.png")).toHaveAttribute("data-status", "done");
  await chat.getByRole("textbox", { name: "Reply to the agent" }).fill("This one.");
  await chat.getByRole("textbox", { name: "Reply to the agent" }).press("Control+Enter");
  await expect(chat.getByTestId("run-chat-reply-sent")).toHaveText("Sent.");
  const mine = chat.getByTestId("run-chat-owner");
  await expect(mine).toContainText("This one.");
  await opensImage(page, mine.getByTestId("run-chat-files"), "screenshot.png");
  expect((await chatOf(me, project, run.id)).messages.map((message) => message.author)).toEqual(["agent", "owner"]);
});

test("a group chat sends a file over 10 MB whole and refuses one over 50 MiB and the eleventh before uploading", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const chat = await createChatByApi(me, { title: uniqueName("Big files") });
  const dir = mkdtempSync(path.join(tmpdir(), "evo-attach-"));
  try {
    const huge = path.join(dir, "huge.bin");
    writeFileSync(huge, "");
    truncateSync(huge, 50 * MIB + 1);
    const big = { name: "dump.bin", mimeType: "application/octet-stream", buffer: Buffer.alloc(12 * MIB, 7) };

    await open(page, chatPath(chat.id));
    const composer = main(page).getByTestId("chat-composer");
    const attach = composer.getByTestId("chat-composer-attach");
    const seen = uploads(page);

    // Over 50 MiB: refused with the reason, nothing uploaded.
    await pick(page, attach, [huge]);
    await expect(composer.getByTestId("attachment-refused")).toHaveAttribute("data-kind", "big");
    await expect(composer.getByTestId("attachment-refused")).toContainText("Not attached: huge.bin. A file is at most 50 MB.");
    await expect(composer.getByTestId("attachment-chip")).toHaveCount(0);
    expect(seen).toHaveLength(0);

    // 12 MB, past the /v1 rewrite's default body limit: uploaded whole.
    await pick(page, attach, [big]);
    await expect(chip(composer, "dump.bin")).toHaveAttribute("data-status", "done", { timeout: 20_000 });
    expect(seen).toHaveLength(1);

    // Ten files at most: the eleventh is refused, named.
    const ten = Array.from({ length: 10 }, (_, index) => ({ ...LOG, name: `part-${index + 1}.log` }));
    await pick(page, attach, ten);
    await expect(composer.getByTestId("attachment-chip")).toHaveCount(10);
    await expect(composer.getByTestId("attachment-refused")).toHaveAttribute("data-kind", "many");
    await expect(composer.getByTestId("attachment-refused")).toContainText("A message carries at most 10 files: part-10.log not attached.");
    await expect.poll(() => seen.length).toBe(10);
    await expect(composer.locator('[data-testid="attachment-chip"][data-status="done"]')).toHaveCount(10);
    for (let index = 1; index <= 9; index += 1) await chip(composer, `part-${index}.log`).getByRole("button", { name: `Remove part-${index}.log` }).click();
    await expect(composer.getByTestId("attachment-chip")).toHaveCount(1);

    await composer.getByTestId("chat-composer-text").fill("The core dump of last night.");
    await composer.getByTestId("chat-composer-text").press("Enter");
    const sent = main(page).getByTestId("chat-log").getByTestId("chat-message").filter({ hasText: "The core dump of last night." });
    await expect(sent.getByTestId("message-attachments").getByTestId("output-chip")).toHaveAccessibleName(/^Open dump\.bin, File, 12 MB$/);
    const [message] = (await chatPageOf(me, chat.id)).messages;
    expect(message.attachments?.map((file) => [file.name, file.size])).toEqual([["dump.bin", 12 * MIB]]);
    const bytes = await page.request.get(message.attachments![0].url);
    expect((await bytes.body()).length).toBe(12 * MIB);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("a failed upload says so and retries; Send waits for it; a failed send keeps the chips", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const chat = await createChatByApi(me, { title: uniqueName("Flaky upload") });
  await open(page, chatPath(chat.id));
  const composer = main(page).getByTestId("chat-composer");
  const isUpload = (url: URL) => url.pathname.endsWith("/attachments");
  await page.route(isUpload, (route) => route.fulfill({ status: 500, json: { error: "server", message: "storage is down", request_id: "rid-1" } }));

  await pick(page, composer.getByTestId("chat-composer-attach"), [NOTES]);
  await expect(chip(composer, "notes.md")).toHaveAttribute("data-status", "failed");
  await expect(chip(composer, "notes.md").getByTestId("attachment-error")).toHaveText("storage is down");
  await composer.getByTestId("chat-composer-text").fill("Notes attached.");
  await composer.getByTestId("chat-composer-send").click();
  await expect(composer.getByTestId("chat-composer-problem")).toHaveText("A file did not upload: retry it or remove it, then send.");
  expect((await chatPageOf(me, chat.id)).messages).toHaveLength(0);

  // Retried: it uploads. The send itself fails once: the words and the chip stay for another try.
  await page.unroute(isUpload);
  await chip(composer, "notes.md").getByRole("button", { name: "Upload notes.md again" }).click();
  await expect(chip(composer, "notes.md")).toHaveAttribute("data-status", "done");
  const isSend = (url: URL) => url.pathname === `/v1/chats/${chat.id}/messages`;
  await page.route(isSend, (route) =>
    route.request().method() === "POST" ? route.fulfill({ status: 503, json: { error: "server", message: "try later", request_id: "rid-2" } }) : route.fallback(),
  );
  await composer.getByTestId("chat-composer-send").click();
  await expect(composer.getByRole("alert").filter({ hasText: /./ }).first()).toBeVisible();
  await expect(chip(composer, "notes.md")).toHaveAttribute("data-status", "done");
  await expect(composer.getByTestId("chat-composer-text")).toHaveValue("Notes attached.");
  await page.unroute(isSend);

  // Send waits for an upload still going: a slow one, then the message goes with both files.
  await page.route(isUpload, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.fallback();
  });
  await pick(page, composer.getByTestId("chat-composer-attach"), [LOG]);
  await expect(chip(composer, "build.log")).toHaveAttribute("data-status", "uploading");
  await expect(chip(composer, "build.log").getByRole("progressbar", { name: "Uploading build.log" })).toBeVisible();
  await composer.getByTestId("chat-composer-send").click();
  await expect(composer.getByTestId("chat-composer-send")).toContainText("Sending");
  const sent = main(page).getByTestId("chat-log").getByTestId("chat-message").filter({ hasText: "Notes attached." });
  await expect(sent.getByTestId("message-attachments").getByTestId("output-chip")).toHaveCount(2);
  await expect(composer.getByTestId("attachment-chip")).toHaveCount(0);
  const [message] = (await chatPageOf(me, chat.id)).messages;
  expect(message.attachments?.map((file) => file.name)).toEqual(["notes.md", "build.log"]);
  // Opened in the viewer: the Markdown as text.
  await sent.getByTestId("message-attachments").getByRole("button", { name: /^Open notes\.md/ }).click();
  await expect(page.getByTestId("output-viewer-name")).toHaveText("notes.md");
  await expect(page.getByTestId("output-viewer-body")).toContainText("The nightly build is red since Tuesday.");
});

test("a reply in a thread and a message to an agent in a direct chat carry files, shown under them", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const project = me.projects[0];
  const chat = await createChatByApi(me, { title: uniqueName("Thread files") });
  const root = await sendByApi(me, chat.id, "Who has the failing screenshot?");
  const answer = await sendByApi(me, chat.id, "Looking for it.", root.id);

  await open(page, chatPath(chat.id, answer.id));
  const thread = page.getByTestId("chat-thread");
  await expect(thread).toHaveAttribute("data-root", String(root.id));
  await drop(page, thread.getByTestId("chat-thread-drop"), [SHOT]);
  await expect(chip(thread, "screenshot.png")).toHaveAttribute("data-status", "done");
  await thread.getByTestId("thread-composer-send").click(); // a file and no words
  const reply = thread.getByTestId("chat-message").filter({ has: page.getByTestId("message-attachments") });
  await expect(reply).toHaveCount(1);
  await opensImage(page, reply.getByTestId("message-attachments"), "screenshot.png");
  expect((await chatPageOf(me, chat.id, root.id)).messages.map((message) => [message.body, message.attachments?.length ?? 0])).toEqual([
    ["Who has the failing screenshot?", 0],
    ["Looking for it.", 0],
    ["", 1],
  ]);

  const handle = uniqueName("reviewer");
  await createAgentByApi(me, project, { handle, name: "Code reviewer", color: "#16a34a" });
  const direct = await openDirectByApi(me, handle);
  await open(page, chatPath(direct.id));
  const box = main(page).getByTestId("chat-composer-text");
  await paste(page, box, [SHOT]);
  await expect(chip(main(page).getByTestId("chat-composer"), "screenshot.png")).toHaveAttribute("data-status", "done");
  await box.fill("Why is the button cut off?");
  await box.press("Enter");
  const asked = main(page).getByTestId("chat-log").getByTestId("chat-message").filter({ hasText: "Why is the button cut off?" });
  await opensImage(page, asked.getByTestId("message-attachments"), "screenshot.png");
});

test("by keyboard alone and at 375 px: attach, remove and send, with nothing scrolling sideways", async ({ page, member }) => {
  const me = await member([{ role: "admin", maxLevel: "internal" }]);
  const chat = await createChatByApi(me, { title: uniqueName("Phone files") });
  await page.setViewportSize({ width: 375, height: 800 });
  await open(page, chatPath(chat.id));
  const composer = main(page).getByTestId("chat-composer");
  const box = composer.getByTestId("chat-composer-text");
  await box.focus();

  // Tab reaches Attach after the box; Enter opens the picker.
  await page.keyboard.press("Tab");
  const attach = composer.getByTestId("chat-composer-attach");
  await expect(attach).toBeFocused();
  await expect(attach).toBeInViewport();
  const box375 = await attach.boundingBox();
  expect(box375!.height).toBeGreaterThanOrEqual(44);
  await pick(page, attach, [{ ...LOG, name: "a-rather-long-unbroken-file-name-that-never-wraps-in-a-chip.log" }, SHOT], true);
  await expect(composer.getByTestId("attachment-chip")).toHaveCount(2);
  await expect(composer.locator('[data-testid="attachment-chip"][data-status="done"]')).toHaveCount(2);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
  await expectNoSeriousViolations(page, "a chat's composer with two files attached at 375 px");

  // Shift Tab from the box reaches the last chip's Remove; Enter removes it.
  await box.focus();
  await page.keyboard.press("Shift+Tab");
  await expect(composer.getByRole("button", { name: "Remove screenshot.png" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(composer.getByTestId("attachment-chip")).toHaveCount(1);

  await box.focus();
  await page.keyboard.type("The log, from my phone.");
  await page.keyboard.press("Control+Enter");
  const sent = main(page).getByTestId("chat-log").getByTestId("chat-message").filter({ hasText: "The log, from my phone." });
  await expect(sent.getByTestId("message-attachments").getByTestId("output-chip")).toHaveCount(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);

  // The file opens from the keyboard too.
  await sent.getByTestId("message-attachments").getByTestId("output-chip").focus();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("output-viewer-name")).toHaveText("a-rather-long-unbroken-file-name-that-never-wraps-in-a-chip.log");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("output-viewer")).toHaveCount(0);
});
